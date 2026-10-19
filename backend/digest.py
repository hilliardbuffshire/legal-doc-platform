"""
서류 전체 판독(digest) + 전문 검색

업로드된 PDF를 처음부터 끝까지 읽어 저장한다.
- 텍스트 레이어가 있는 쪽은 그대로, 스캔 이미지 쪽은 Claude가 PDF를 직접 읽음
- 25쪽 단위 '구간 상세요약'(숫자·이름·날짜 보존) + 문서 전체 요약(1,200자 이내)
- 다른 파일과 똑같은 쪽(법원 기록 중복 다운로드)은 다시 읽지 않고 건너뜀
- /api/search 로 질문과 관련된 구간 요약·원문 쪽을 찾아 준다 (헬퍼가 답변 근거로 사용)

저장 위치: DATA_DIR/text/{file_id}.json  +  SQLite doc_digest / page_hash 테이블
"""
import asyncio, base64, json, math, re, time
from pathlib import Path

import fitz

CHUNK_PAGES = 25      # 한 번에 읽을 쪽 수 (스캔 PDF 25쪽 ≈ 10MB 이하)
TEXT_OK     = 150     # 워터마크 제외 글자 수가 이 이상이면 텍스트로 읽을 수 있는 쪽
DUP_BITS    = 3       # 이미지 쪽 중복 판정 허용 비트 차이(256비트 중). 같은 기록 재다운로드는 0, 다른 빈 쪽끼리는 8 이상
PARALLEL    = 3       # 동시 판독 요청 수

CHUNK_PROMPT = """당신은 한국 민사소송 기록을 정리하는 법률 보조원입니다. 아래는 서류 「{title}」의 일부(원본 쪽번호: {pr})입니다.
{hint}
이 쪽들에 실제로 들어 있는 내용만 근거로, 나중에 어떤 질문에도 답할 수 있도록 '사실 중심 상세 요약'을 작성하세요. 추측 금지, 안 보이는 건 쓰지 마세요.
형식:
■ 포함된 문서: 문서마다 한 줄 — [원본 p.x~y] 문서종류 / 작성·제출자 / 날짜 / 증거번호(갑·을 제○호증 등)
■ 핵심 내용: 문서별로 주장·사실관계·법원 판단·감정/조사 결과를 bullet로. 금액(원 단위 그대로), 날짜, 사람·업체 이름, 지번·면적, 오염물질명·수치, 공사내역, 통화·발언 내용은 숫자·표현 그대로 빠짐없이.
■ 원고에게 유리/불리한 포인트: 기록에 나타난 것만 짧게.
반복되는 표지·워터마크·빈 페이지는 무시. 3,500자 이내로 압축하되 숫자·고유명사는 생략하지 마세요."""

SUMMARY_PROMPT = """아래는 서류 「{title}」({pages}쪽)를 구간별로 읽고 정리한 상세 요약입니다.
이 서류 전체를 1,200자 이내로 요약하세요.
- 첫 줄: 이 파일에 들어 있는 문서들(종류·제출자·날짜)을 한 줄로
- 이어서 핵심 사실·주장·판단을 bullet로. 금액·날짜·이름·수치는 원문 그대로
- 원고(정상문)에게 특히 중요한 점이 있으면 마지막에 한 줄
설명·인사 없이 요약만 출력.

{body}"""


def _dhash(page) -> int:
    """쪽 이미지 지문(상단 8% 워터마크 띠 제외). 같은 기록을 다른 날 내려받아도 같은 값."""
    pm = page.get_pixmap(matrix=fitz.Matrix(0.12, 0.12), colorspace=fitz.csGRAY)
    w, h, s = pm.width, pm.height, pm.samples
    y0, bits = int(h * 0.08), 0
    for gy in range(16):
        y = y0 + int((h - y0 - 1) * gy / 15)
        row = [s[y * w + int((w - 1) * gx / 16)] for gx in range(17)]
        for i in range(16):
            bits = (bits << 1) | (1 if row[i] > row[i + 1] else 0)
    return bits


def _norm_text(t: str) -> str:
    return re.sub(r"\s+", "", t)


# ── 검색용 토큰화: 한글은 2글자 조각, 숫자는 쉼표 제거 ─────────────────
def _tokens(s: str) -> list:
    out = []
    for w in re.findall(r"[가-힣]+|[A-Za-z]+|\d[\d,\.]*\d|\d", s):
        if "가" <= w[0] <= "힣":
            out += [w] if len(w) == 1 else [w[i:i + 2] for i in range(len(w) - 1)]
        else:
            out.append(w.lower().replace(",", ""))
    return out


class Digester:
    def __init__(self, ai, db_conn, data_dir: Path, upload_dir: Path, load_meta, clean,
                 model: str, hint: str = ""):
        self.ai, self.db, self.load_meta, self.clean = ai, db_conn, load_meta, clean
        self.upload_dir, self.model, self.hint = upload_dir, model, hint
        self.text_dir = data_dir / "text"
        self.text_dir.mkdir(parents=True, exist_ok=True)
        self.queue: asyncio.Queue = asyncio.Queue()
        self.queued: set = set()
        self._index = None
        with self.db() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS doc_digest (
                file_id TEXT PRIMARY KEY, status TEXT DEFAULT 'pending',
                done INTEGER DEFAULT 0, total INTEGER DEFAULT 0, error TEXT DEFAULT '',
                summary TEXT DEFAULT '', in_tok INTEGER DEFAULT 0, out_tok INTEGER DEFAULT 0,
                updated REAL DEFAULT 0)""")
            con.execute("""CREATE TABLE IF NOT EXISTS page_hash (
                file_id TEXT, page INTEGER, hash TEXT, text_key TEXT,
                PRIMARY KEY(file_id, page))""")

    # ── 상태 ─────────────────────────────────────────────────────────
    def _set(self, fid: str, **kw):
        kw["updated"] = time.time()
        cols = ", ".join(kw)
        upd = ", ".join(f"{k}=excluded.{k}" for k in kw)
        with self.db() as con:
            con.execute(f"INSERT INTO doc_digest (file_id, {cols}) VALUES (?, {', '.join('?' * len(kw))}) "
                        f"ON CONFLICT(file_id) DO UPDATE SET {upd}", [fid] + list(kw.values()))

    def _add_tokens(self, fid: str, i: int, o: int):
        with self.db() as con:
            con.execute("UPDATE doc_digest SET in_tok=in_tok+?, out_tok=out_tok+? WHERE file_id=?", [i, o, fid])

    def status_map(self) -> dict:
        with self.db() as con:
            rows = con.execute("SELECT file_id, status, done, total, error, summary, in_tok, out_tok FROM doc_digest").fetchall()
        return {r[0]: {"status": r[1], "done": r[2], "total": r[3], "error": r[4],
                       "summary": r[5], "in_tok": r[6], "out_tok": r[7]} for r in rows}

    def _load(self, fid: str) -> dict:
        p = self.text_dir / f"{fid}.json"
        return json.loads(p.read_text("utf-8")) if p.exists() else {"pages": [], "chunks": [], "dups": {}}

    def _save(self, fid: str, d: dict):
        (self.text_dir / f"{fid}.json").write_text(json.dumps(d, ensure_ascii=False), "utf-8")
        self._index = None

    def forget(self, fid: str):
        p = self.text_dir / f"{fid}.json"
        if p.exists():
            p.unlink()
        with self.db() as con:
            con.execute("DELETE FROM doc_digest WHERE file_id=?", [fid])
            con.execute("DELETE FROM page_hash WHERE file_id=?", [fid])
        self._index = None

    # ── 큐 ───────────────────────────────────────────────────────────
    def enqueue(self, fid: str):
        if fid not in self.queued:
            self.queued.add(fid)
            st = self.status_map().get(fid, {})
            if st.get("status") not in ("done", "summarizing"):
                self._set(fid, status="pending", error="")
            self.queue.put_nowait(fid)

    def enqueue_unfinished(self):
        """재시작 시 진행 중이던 판독만 이어서 (기존 서류 일괄 판독은 관리자 reprocess로만 — 비용 통제)"""
        meta = self.load_meta()
        for fid, v in self.status_map().items():
            if fid in meta and v["status"] in ("pending", "running", "summarizing"):
                self.enqueue(fid)

    async def worker(self):
        while True:
            fid = await self.queue.get()
            try:
                await self.process(fid)
            except Exception as e:
                self._set(fid, status="error", error=str(e)[:500])
            finally:
                self.queued.discard(fid)

    # ── 판독 ─────────────────────────────────────────────────────────
    def _scan_pages(self, fid: str, doc) -> list:
        pages = []
        with self.db() as con:
            for i, pg in enumerate(doc):
                text = self.clean(pg.get_text().strip())
                key = _norm_text(text)[:2000] if len(text) >= TEXT_OK else ""
                h = format(_dhash(pg), "064x")
                pages.append({"page": i + 1, "text": text, "hash": h, "key": key})
                con.execute("INSERT OR REPLACE INTO page_hash (file_id, page, hash, text_key) VALUES (?,?,?,?)",
                            [fid, i + 1, h, key])
        return pages

    def _find_dups(self, fid: str, pages: list) -> dict:
        """이미 판독 완료된 다른 파일에 똑같은 쪽이 있으면 {쪽: [그 파일, 그 쪽]}"""
        done = [k for k, v in self.status_map().items() if v["status"] == "done" and k != fid]
        if not done:
            return {}
        with self.db() as con:
            rows = con.execute(f"SELECT file_id, page, hash, text_key FROM page_hash WHERE file_id IN "
                               f"({','.join('?' * len(done))})", done).fetchall()
        by_key = {r[3]: (r[0], r[1]) for r in rows if r[3]}
        img = [(int(r[2], 16), r[0], r[1]) for r in rows if not r[3]]
        dups = {}
        for p in pages:
            if p["key"]:
                if p["key"] in by_key:
                    dups[str(p["page"])] = list(by_key[p["key"]])
                continue
            h = int(p["hash"], 16)
            for oh, ofid, opg in img:
                if bin(h ^ oh).count("1") <= DUP_BITS:
                    dups[str(p["page"])] = [ofid, opg]
                    break
        return dups

    async def _ask(self, fid: str, content, max_tokens: int) -> str:
        r = await self.ai.messages.create(model=self.model, max_tokens=max_tokens,
                                          messages=[{"role": "user", "content": content}], timeout=900)
        self._add_tokens(fid, r.usage.input_tokens, r.usage.output_tokens)
        return "".join(c.text for c in r.content if c.type == "text").strip()

    async def _digest_chunk(self, fid: str, title: str, doc, pages: list, nums: list) -> str:
        pr = f"{nums[0]}~{nums[-1]}" if nums == list(range(nums[0], nums[-1] + 1)) else ",".join(map(str, nums))
        prompt = CHUNK_PROMPT.format(title=title, pr=pr, hint=self.hint)
        if all(len(pages[n - 1]["text"]) >= TEXT_OK for n in nums):
            body = "\n\n".join(f"=====[원본 p.{n}]=====\n{pages[n - 1]['text']}" for n in nums)
            return await self._ask(fid, prompt + "\n\n" + body, 8000)
        sub = fitz.open()
        for n in nums:
            sub.insert_pdf(doc, from_page=n - 1, to_page=n - 1)
        data = base64.b64encode(sub.tobytes(garbage=3, deflate=True)).decode()
        sub.close()
        return await self._ask(fid, [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}},
            {"type": "text", "text": prompt}], 8000)

    async def summarize(self, fid: str, title: str, total: int, d: dict) -> str:
        body = "\n\n".join(f"[구간 원본 p.{c['pages'][0]}~{c['pages'][-1]}]\n{c['text']}" for c in d["chunks"])
        if not body:
            return ""
        return await self._ask(fid, SUMMARY_PROMPT.format(title=title, pages=total, body=body[:400000]), 3000)

    async def process(self, fid: str):
        meta = self.load_meta()
        info = meta.get(fid)
        if not info:
            return
        if info.get("ext", "pdf") != "pdf":
            self._set(fid, status="skip", error="PDF가 아닌 파일은 판독하지 않음")
            return
        path = self.upload_dir / f"{fid}.pdf"
        title = info["file_name"]
        if self.status_map().get(fid, {}).get("status") == "summarizing":   # 가져온 구간 요약 → 전체 요약만 생성
            d = self._load(fid)
            self._set(fid, status="done", summary=await self.summarize(fid, title, len(d["pages"]), d), error="")
            return
        doc = fitz.open(str(path))
        try:
            d = self._load(fid)
            pages = await asyncio.to_thread(self._scan_pages, fid, doc)
            d["pages"] = [{"page": p["page"], "text": p["text"]} for p in pages]
            # 이미 읽은 쪽은 그대로 두고(중복 판정에서도 제외), 아직 안 읽은 쪽만 새로 읽음 → 재판독해도 추가 비용은 새 쪽만
            covered = {n for c in d["chunks"] for n in c["pages"]}
            dups = await asyncio.to_thread(self._find_dups, fid, pages)
            d["dups"] = {k: v for k, v in dups.items() if int(k) not in covered}
            todo = [p["page"] for p in pages if str(p["page"]) not in d["dups"] and p["page"] not in covered]
            groups = [todo[i:i + CHUNK_PAGES] for i in range(0, len(todo), CHUNK_PAGES)]
            old_summary = self.status_map().get(fid, {}).get("summary", "")
            self._set(fid, status="running", total=len(d["chunks"]) + len(groups), done=len(d["chunks"]), error="")
            self._save(fid, d)

            sem = asyncio.Semaphore(PARALLEL)

            async def one(g):
                async with sem:
                    text = await self._digest_chunk(fid, title, doc, pages, g)
                d["chunks"].append({"pages": g, "text": text})
                d["chunks"].sort(key=lambda c: c["pages"][0])
                self._save(fid, d)
                with self.db() as con:
                    con.execute("UPDATE doc_digest SET done=done+1, updated=? WHERE file_id=?", [time.time(), fid])

            await asyncio.gather(*[one(g) for g in groups])

            if groups or (d["chunks"] and not old_summary):
                summary = await self.summarize(fid, title, len(pages), d)
            elif d["chunks"]:
                summary = old_summary
            else:
                refs = sorted({meta.get(v[0], {}).get("file_name", v[0])[:60] for v in d["dups"].values()})
                summary = "이 파일의 모든 쪽은 다음 서류와 내용이 같습니다(중복 다운로드): " + " / ".join(refs)
            self._set(fid, status="done", summary=summary, error="")
            self._save(fid, d)
        finally:
            doc.close()

    def import_chunks(self, fid: str, chunks: list, summary: str = "", dups: dict = None):
        """이미 다른 곳에서 읽어 둔 구간 요약을 그대로 등록 (재판독 비용 절약). summary 없으면 큐에서 전체 요약만 생성"""
        path = self.upload_dir / f"{fid}.pdf"
        doc = fitz.open(str(path))
        try:
            pages = self._scan_pages(fid, doc)
        finally:
            doc.close()
        d = {"pages": [{"page": p["page"], "text": p["text"]} for p in pages],
             "chunks": sorted(chunks, key=lambda c: c["pages"][0]), "dups": dups or {}}
        self._save(fid, d)
        self._set(fid, status="done" if summary else "summarizing", total=len(chunks), done=len(chunks),
                  summary=summary, error="")
        if not summary:
            self.enqueue(fid)

    # ── 검색 ─────────────────────────────────────────────────────────
    def _build_index(self):
        meta = self.load_meta()
        st = self.status_map()
        units = []
        for fid, info in meta.items():
            p = self.text_dir / f"{fid}.json"
            if not p.exists():
                continue
            d = json.loads(p.read_text("utf-8"))
            title = info.get("file_name", fid)
            for c in d.get("chunks", []):
                units.append({"fid": fid, "title": title, "kind": "요약",
                              "pages": f"{c['pages'][0]}~{c['pages'][-1]}", "text": c["text"]})
            for pg in d.get("pages", []):
                if len(pg["text"]) >= 50 and str(pg["page"]) not in d.get("dups", {}):
                    units.append({"fid": fid, "title": title, "kind": "원문",
                                  "pages": str(pg["page"]), "text": pg["text"]})
            if st.get(fid, {}).get("summary"):
                units.append({"fid": fid, "title": title, "kind": "전체요약", "pages": "전체",
                              "text": st[fid]["summary"]})
        df, toks = {}, []
        for u in units:
            t = _tokens(u["title"] + " " + u["text"])
            tf = {}
            for w in t:
                tf[w] = tf.get(w, 0) + 1
            toks.append((tf, len(t)))
            for w in tf:
                df[w] = df.get(w, 0) + 1
        avg = sum(n for _, n in toks) / max(len(toks), 1)
        self._index = (units, toks, df, avg)

    def search(self, q: str, k: int = 10, budget: int = 30000) -> list:
        if self._index is None:
            self._build_index()
        units, toks, df, avg = self._index
        qt = set(_tokens(q))
        if not qt or not units:
            return []
        N = len(units)
        scores = []
        for i, (tf, n) in enumerate(toks):
            s = 0.0
            for w in qt:
                f = tf.get(w)
                if f:
                    idf = math.log(1 + (N - df[w] + 0.5) / (df[w] + 0.5))
                    s += idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * n / avg))
            if s > 0:
                scores.append((s * (1.2 if units[i]["kind"] != "원문" else 1.0), i))
        scores.sort(reverse=True)
        out, used = [], 0
        for s, i in scores[:k * 3]:
            u = units[i]
            text = u["text"][:6000]
            if used + len(text) > budget and out:
                break
            out.append({**{kk: u[kk] for kk in ("fid", "title", "kind", "pages")}, "score": round(s, 2), "text": text})
            used += len(text)
            if len(out) >= k:
                break
        return out

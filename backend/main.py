"""
법률 문서 검색 플랫폼 — 백엔드 v3
FastAPI + ONNX 임베딩 (한국어) + Anthropic Claude + SQLite (별점·메모·요약 영속 저장)
"""
import os, io, json, hashlib, pickle, asyncio, warnings, sqlite3, time
from pathlib import Path
from typing import AsyncGenerator
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

warnings.filterwarnings("ignore")

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
import fitz  # PyMuPDF — 한국어 UniKS 인코딩 지원
from pypdf import PdfReader  # fitz 폴백용
import numpy as np
import anthropic

load_dotenv()

# ── 설정 ──────────────────────────────────────────────────────────────
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CHAT_MODEL    = os.getenv("CLAUDE_MODEL", "claude-opus-4-5")
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", CHAT_MODEL)
CHUNK_SIZE    = 700
CHUNK_OVR     = 80
TOP_K         = 8

ai    = anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY or "placeholder")
_pool = ThreadPoolExecutor(max_workers=2)

# ── 영속 저장소 ───────────────────────────────────────────────────────
DATA_DIR   = Path(os.getenv("DATA_DIR", "."))
UPLOAD_DIR = DATA_DIR / "uploads"
META       = DATA_DIR / "meta.json"
STORE      = DATA_DIR / "vector_store.pkl"
DB_PATH    = DATA_DIR / "db.sqlite"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ── SQLite (별점·메모·요약·수정시각) ──────────────────────────────────
def _db_conn() -> sqlite3.Connection:
    return sqlite3.connect(str(DB_PATH), check_same_thread=False)

def init_db():
    with _db_conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS file_meta (
                file_id    TEXT PRIMARY KEY,
                star       INTEGER DEFAULT 0,
                comment    TEXT    DEFAULT '',
                summary    TEXT    DEFAULT '',
                updated_at REAL    DEFAULT 0
            )
        """)

init_db()

def db_get_all() -> dict[str, dict]:
    with _db_conn() as con:
        rows = con.execute(
            "SELECT file_id, star, comment, summary, updated_at FROM file_meta"
        ).fetchall()
    return {
        r[0]: {"star": r[1], "comment": r[2], "summary": r[3], "updated_at": r[4]}
        for r in rows
    }

def db_upsert(file_id: str, **kwargs):
    """지정한 컬럼만 갱신. updated_at 은 항상 현재 시각으로 설정."""
    kwargs["updated_at"] = time.time()
    cols   = ", ".join(kwargs.keys())
    ph     = ", ".join("?" * len(kwargs))
    update = ", ".join(f"{k}=excluded.{k}" for k in kwargs)
    vals   = [file_id] + list(kwargs.values())
    with _db_conn() as con:
        con.execute(
            f"INSERT INTO file_meta (file_id, {cols}) VALUES (?, {ph}) "
            f"ON CONFLICT(file_id) DO UPDATE SET {update}",
            vals,
        )

# ── 임베딩 모델 ────────────────────────────────────────────────────────
from embedder import Embedder
_embedder = Embedder(DATA_DIR)

def _embed_sync(text: str) -> list[float]:
    return _embedder.embed(text)

async def embed(text: str) -> list[float]:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_pool, _embed_sync, text)

# ── 메타/벡터 저장소 ──────────────────────────────────────────────────
def load_meta() -> dict:
    return json.loads(META.read_text("utf-8")) if META.exists() else {}

def save_meta(m: dict):
    META.write_text(json.dumps(m, ensure_ascii=False, indent=2), "utf-8")

def load_store() -> list:
    return pickle.loads(STORE.read_bytes()) if STORE.exists() else []

def save_store(store: list):
    STORE.write_bytes(pickle.dumps(store))

# ── 벡터 검색 ─────────────────────────────────────────────────────────
def cosine_search(store: list, query_vec: list[float], n: int) -> list[dict]:
    if not store:
        return []
    q   = np.array(query_vec, dtype=np.float32)
    q  /= np.linalg.norm(q) + 1e-10
    mat = np.array([c["embedding"] for c in store], dtype=np.float32)
    mat /= np.linalg.norm(mat, axis=1, keepdims=True) + 1e-10
    sims = mat @ q
    top  = np.argsort(sims)[::-1][:n]
    return [{"chunk": store[i], "score": float(sims[i])} for i in top]

# ── PDF 처리 ──────────────────────────────────────────────────────────
# 법원 시스템 워터마크 (이 키워드만 있는 페이지는 내용 없음으로 처리)
_WATERMARK = ['개인정보유출주의', '다운로드일시', '제출자:', 'scourt.go.kr']

def _clean(raw: str) -> str:
    """워터마크 전용 줄 제거. 실질 내용이 한 줄이라도 있으면 그대로 반환."""
    lines = [l.strip() for l in raw.split('\n') if l.strip()]
    content = [l for l in lines if not any(w in l for w in _WATERMARK)]
    return '\n'.join(content) if content else ''

def _extract_fitz(pdf_bytes: bytes) -> list[dict]:
    """fitz(PyMuPDF) 로 페이지별 텍스트 추출."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    result = []
    for i in range(len(doc)):
        raw = doc[i].get_text().strip()
        text = _clean(raw)
        if text:
            result.append({"page": i + 1, "text": text})
    doc.close()
    return result

def _extract_pypdf(pdf_bytes: bytes) -> list[dict]:
    """pypdf 폴백 — fitz가 실패한 경우 사용."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        result = []
        for i, p in enumerate(reader.pages):
            raw = (p.extract_text() or "").strip()
            text = _clean(raw)
            if text:
                result.append({"page": i + 1, "text": text})
        return result
    except Exception:
        return []

def _page_count(pdf_bytes: bytes) -> int:
    """실제 PDF 총 페이지 수 (텍스트 유무 무관)."""
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        n = len(doc)
        doc.close()
        return n
    except Exception:
        return 0

def extract_pages(pdf_bytes: bytes) -> tuple[list[dict], bool]:
    """
    텍스트 페이지 목록과 스캔 여부를 반환.
    fitz 우선 → 결과 없으면 pypdf 폴백.
    둘 다 실패해도 빈 리스트 반환 (업로드는 허용).
    """
    pages = _extract_fitz(pdf_bytes)
    if not pages:
        pages = _extract_pypdf(pdf_bytes)
    is_scanned = len(pages) == 0
    return pages, is_scanned

def make_chunks(fid: str, fname: str, page: int, text: str) -> list[dict]:
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()] or [text]
    chunks, buf, idx = [], "", 0

    def flush():
        nonlocal buf, idx
        if buf.strip():
            chunks.append({
                "id":   f"{fid}_p{page}_c{idx}",
                "text": buf.strip(),
                "meta": {"file_id": fid, "file_name": fname, "page": page},
            })
            idx += 1
        buf = ""

    for para in paragraphs:
        if len(buf) + len(para) <= CHUNK_SIZE:
            buf += ("\n\n" if buf else "") + para
        else:
            flush()
            if len(para) > CHUNK_SIZE:
                for s in range(0, len(para), CHUNK_SIZE - CHUNK_OVR):
                    piece = para[s: s + CHUNK_SIZE]
                    if piece.strip():
                        chunks.append({
                            "id":   f"{fid}_p{page}_c{idx}",
                            "text": piece.strip(),
                            "meta": {"file_id": fid, "file_name": fname, "page": page},
                        })
                        idx += 1
            else:
                buf = para
    flush()
    return chunks

# ── AI 요약 생성 (업로드 시 1회) ──────────────────────────────────────
async def generate_summary(pages: list[dict]) -> str:
    preview = "\n\n".join(p["text"] for p in pages[:5])[:3000]
    try:
        msg = await ai.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": (
                    "다음 법률 문서의 핵심 내용을 한국어로 3문장으로 요약해 주세요. "
                    "문서 종류, 주요 주장, 핵심 결론을 포함하고, 반드시 마침표로 끝나는 완전한 문장으로 작성하세요.\n\n"
                    + preview
                ),
            }],
        )
        return msg.content[0].text.strip()
    except Exception:
        return ""

# ── FastAPI ───────────────────────────────────────────────────────────
app = FastAPI(title="법률 문서 플랫폼", version="3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ── GET /api/files ─────────────────────────────────────────────────────
@app.get("/api/files")
def get_files():
    meta   = load_meta()
    db_map = db_get_all()
    result = []
    for fid, info in meta.items():
        row = db_map.get(fid, {})
        result.append({
            **info,
            "star":       row.get("star", 0),
            "comment":    row.get("comment", ""),
            "summary":    row.get("summary", ""),
            "updated_at": row.get("updated_at", 0),
        })
    return sorted(result, key=lambda x: x["updated_at"], reverse=True)

# ── POST /api/upload ──────────────────────────────────────────────────
@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "PDF 파일만 업로드 가능합니다.")

    data = await file.read()
    fid  = hashlib.md5(file.filename.encode()).hexdigest()
    meta = load_meta()

    if fid in meta:
        return {"skipped": True, "file": meta[fid], "message": "이미 처리된 파일입니다."}

    pages, is_scanned = extract_pages(data)

    # 텍스트 추출 실패(스캔 PDF)여도 업로드 허용 — 열람은 가능
    (UPLOAD_DIR / f"{fid}.pdf").write_bytes(data)

    all_chunks = []
    if pages:
        for p in pages:
            all_chunks.extend(make_chunks(fid, file.filename, p["page"], p["text"]))
        store = load_store()
        existing_ids = {c["id"] for c in store}
        for c in all_chunks:
            if c["id"] not in existing_ids:
                c["embedding"] = await embed(c["text"])
                store.append(c)
        save_store(store)

    summary = (await generate_summary(pages)) if pages else "이미지 스캔 PDF — 텍스트 검색 불가, 열람만 가능합니다."

    info = {
        "file_id":     fid,
        "file_name":   file.filename,
        "total_pages": _page_count(data),
        "chunks":      len(all_chunks),
        "is_scanned":  is_scanned,
    }
    meta[fid] = info
    save_meta(meta)
    db_upsert(fid, summary=summary)

    msg = f"업로드 완료 ({len(all_chunks)}개 청크)" if not is_scanned else "업로드 완료 (이미지 스캔 — 검색 불가, 열람 가능)"
    return {"skipped": False, "file": info, "message": msg}

# ── GET /api/files/{fid}/pdf ──────────────────────────────────────────
@app.get("/api/files/{fid}/pdf")
def serve_pdf(fid: str, dl: bool = Query(False)):
    meta = load_meta()
    if fid not in meta:
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    pdf_path = UPLOAD_DIR / f"{fid}.pdf"
    if not pdf_path.exists():
        raise HTTPException(404, "파일이 서버에 없습니다. 다시 업로드해 주세요.")
    fname       = meta[fid]["file_name"]
    disposition = "attachment" if dl else "inline"
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(fname)}"},
    )

# ── DELETE /api/files/{fid} ───────────────────────────────────────────
@app.delete("/api/files/{fid}")
def delete_file(fid: str):
    meta = load_meta()
    if fid not in meta:
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    store = [c for c in load_store() if c["meta"]["file_id"] != fid]
    save_store(store)
    pdf_path = UPLOAD_DIR / f"{fid}.pdf"
    if pdf_path.exists():
        pdf_path.unlink()
    del meta[fid]
    save_meta(meta)
    with _db_conn() as con:
        con.execute("DELETE FROM file_meta WHERE file_id = ?", [fid])
    return {"ok": True}

# ── PATCH /api/files/{fid}/name ───────────────────────────────────────
class NameReq(BaseModel):
    name: str

@app.patch("/api/files/{fid}/name")
def patch_name(fid: str, req: NameReq):
    new_name = req.name.strip()
    if not new_name:
        raise HTTPException(400, "파일명이 비어 있습니다.")
    if not new_name.lower().endswith(".pdf"):
        new_name += ".pdf"

    meta = load_meta()
    if fid not in meta:
        raise HTTPException(404, "파일을 찾을 수 없습니다.")

    # meta.json 업데이트
    meta[fid]["file_name"] = new_name
    save_meta(meta)

    # 벡터 스토어 안 모든 청크의 file_name 동기화
    store = load_store()
    for chunk in store:
        if chunk["meta"]["file_id"] == fid:
            chunk["meta"]["file_name"] = new_name
    save_store(store)

    db_upsert(fid)  # updated_at 갱신
    return {"ok": True, "file_name": new_name}

# ── PATCH /api/files/{fid}/star ───────────────────────────────────────
class StarReq(BaseModel):
    star: int  # 0(초기화)~5

@app.patch("/api/files/{fid}/star")
def patch_star(fid: str, req: StarReq):
    if fid not in load_meta():
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    if not (0 <= req.star <= 5):
        raise HTTPException(400, "별점은 0~5 사이여야 합니다.")
    db_upsert(fid, star=req.star)
    return {"ok": True, "star": req.star}

# ── PATCH /api/files/{fid}/comment ───────────────────────────────────
class CommentReq(BaseModel):
    comment: str

@app.patch("/api/files/{fid}/comment")
def patch_comment(fid: str, req: CommentReq):
    if fid not in load_meta():
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    db_upsert(fid, comment=req.comment[:200])
    return {"ok": True, "comment": req.comment[:200]}

# ── GET /api/search ───────────────────────────────────────────────────
@app.get("/api/search")
async def search(q: str = Query(..., min_length=1)):
    store = load_store()
    if not store:
        return {"query": q, "results": []}

    hits_raw = cosine_search(store, await embed(q), n=min(80, len(store)))

    doc_map: dict = {}
    for h in hits_raw:
        m   = h["chunk"]["meta"]
        fid = m["file_id"]
        if fid not in doc_map or h["score"] > doc_map[fid]["score"]:
            doc_map[fid] = {
                "text":      h["chunk"]["text"],
                "file_name": m["file_name"],
                "file_id":   fid,
                "page":      m["page"],
                "score":     round(h["score"], 3),
            }

    results = sorted(doc_map.values(), key=lambda x: x["score"], reverse=True)[:10]
    return {"query": q, "results": results}

# ── POST /api/chat (Streaming SSE) ────────────────────────────────────
class ChatReq(BaseModel):
    question: str

@app.post("/api/chat")
async def chat(req: ChatReq):
    async def gen() -> AsyncGenerator[str, None]:
        store = load_store()
        if not store:
            yield f"data: {json.dumps({'type':'text','content':'아직 업로드된 문서가 없습니다. 먼저 PDF를 업로드해 주세요.'}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type':'done'})}\n\n"
            return

        hits = cosine_search(store, await embed(req.question), n=min(TOP_K, len(store)))

        context_parts, sources = [], {}
        for h in hits:
            m = h["chunk"]["meta"]
            context_parts.append(f"[{m['file_name']} · {m['page']}페이지]\n{h['chunk']['text']}")
            key = f"{m['file_name']}|{m['page']}"
            sources[key] = {"file_name": m["file_name"], "file_id": m["file_id"], "page": m["page"]}

        context = "\n\n---\n\n".join(context_parts)

        system_prompt = (
            "당신은 한국 법률 소송 문서 전문 AI 어시스턴트입니다. "
            "사건번호 2023가단62004, 2025가단4471 관련 문서들을 분석합니다. "
            "주어진 문서 내용만을 근거로 정확하게 한국어로 답변하세요. "
            "답변할 때 '[번호]번 문서'처럼 문서 번호를 구체적으로 언급하세요. "
            "문서에 없는 내용은 '해당 문서에서 확인되지 않습니다'라고 명시하세요. "
            "법률 용어는 정확하게 사용하고, 간결하고 명확하게 답변하세요."
        )
        user_content = (
            f"[참고 문서]\n{context}\n\n"
            f"[질문]\n{req.question}\n\n"
            "※ 답변 끝에 반드시 출처(파일명, 페이지)를 명시하세요."
        )

        async with ai.messages.stream(
            model=CHAT_MODEL,
            max_tokens=4096,
            system=system_prompt,
            messages=[{"role": "user", "content": user_content}],
        ) as stream:
            async for text in stream.text_stream:
                yield f"data: {json.dumps({'type':'text','content':text}, ensure_ascii=False)}\n\n"

        yield f"data: {json.dumps({'type':'sources','sources':list(sources.values())}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'type':'done'})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")

# ── 프론트엔드 정적 파일 서빙 ─────────────────────────────────────────
for _dist in [Path("./frontend_dist"), Path("../frontend/dist")]:
    if _dist.exists():
        app.mount("/", StaticFiles(directory=str(_dist), html=True), name="static")
        break

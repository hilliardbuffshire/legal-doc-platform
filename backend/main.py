"""
법률 문서 검색 플랫폼 — 백엔드 v4
FastAPI + Anthropic Claude (AI 요약) + SQLite (별점·메모·요약 영속 저장)
벡터 검색 제거 — 파일명 기반 필터링만 사용
"""
import os, io, json, hashlib, sqlite3, time, asyncio, re
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from dotenv import load_dotenv
import fitz          # PyMuPDF — 한국어 UniKS 인코딩 지원
from pypdf import PdfReader  # fitz 폴백용
import anthropic

load_dotenv()

# ── 설정 ──────────────────────────────────────────────────────────────
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", os.getenv("CLAUDE_MODEL", "claude-opus-4-5"))

ai = anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY or "placeholder")

# ── 영속 저장소 ───────────────────────────────────────────────────────
DATA_DIR   = Path(os.getenv("DATA_DIR", "."))
UPLOAD_DIR = DATA_DIR / "uploads"
META       = DATA_DIR / "meta.json"
DB_PATH    = DATA_DIR / "db.sqlite"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# ── SQLite ─────────────────────────────────────────────────────────────
def _db_conn() -> sqlite3.Connection:
    return sqlite3.connect(str(DB_PATH), check_same_thread=False)

def init_db():
    with _db_conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS file_meta (
                file_id        TEXT PRIMARY KEY,
                star           INTEGER DEFAULT 0,
                comment        TEXT    DEFAULT '',
                lawyer_comment TEXT    DEFAULT '',
                summary        TEXT    DEFAULT '',
                updated_at     REAL    DEFAULT 0
            )
        """)
        # 기존 테이블에 lawyer_comment 컬럼 추가 (없을 경우에만)
        try:
            con.execute("ALTER TABLE file_meta ADD COLUMN lawyer_comment TEXT DEFAULT ''")
        except Exception:
            pass  # 이미 존재하면 무시

init_db()

def db_get_all() -> dict:
    with _db_conn() as con:
        rows = con.execute(
            "SELECT file_id, star, comment, lawyer_comment, summary, updated_at FROM file_meta"
        ).fetchall()
    return {
        r[0]: {
            "star":           r[1],
            "comment":        r[2],
            "lawyer_comment": r[3],
            "summary":        r[4],
            "updated_at":     r[5],
        }
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

# ── 메타 저장소 ──────────────────────────────────────────────────────
def load_meta() -> dict:
    return json.loads(META.read_text("utf-8")) if META.exists() else {}

def save_meta(m: dict):
    META.write_text(json.dumps(m, ensure_ascii=False, indent=2), "utf-8")

# ── PDF 처리 ──────────────────────────────────────────────────────────
_WATERMARK = ['개인정보유출주의', '다운로드일시', '제출자:', 'scourt.go.kr']

def _clean(raw: str) -> str:
    lines   = [l.strip() for l in raw.split('\n') if l.strip()]
    content = [l for l in lines if not any(w in l for w in _WATERMARK)]
    return '\n'.join(content) if content else ''

def _extract_fitz(pdf_bytes: bytes) -> list:
    doc    = fitz.open(stream=pdf_bytes, filetype="pdf")
    result = []
    for i in range(len(doc)):
        text = _clean(doc[i].get_text().strip())
        if text:
            result.append({"page": i + 1, "text": text})
    doc.close()
    return result

def _extract_pypdf(pdf_bytes: bytes) -> list:
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        result = []
        for i, p in enumerate(reader.pages):
            text = _clean((p.extract_text() or "").strip())
            if text:
                result.append({"page": i + 1, "text": text})
        return result
    except Exception:
        return []

def _page_count(pdf_bytes: bytes) -> int:
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        n   = len(doc)
        doc.close()
        return n
    except Exception:
        return 0

def extract_pages(pdf_bytes: bytes) -> tuple:
    pages      = _extract_fitz(pdf_bytes)
    if not pages:
        pages  = _extract_pypdf(pdf_bytes)
    is_scanned = len(pages) == 0
    return pages, is_scanned

# ── 다음 문서 번호 계산 ───────────────────────────────────────────────
def next_doc_number(meta: dict) -> int:
    """meta.json 내 최대 [N] 번호 + 1"""
    nums = []
    for info in meta.values():
        m = re.match(r'^\[(\d+)\]', info.get('file_name', ''))
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


# ── 제목 자동 생성 (플랫폼 형식) ─────────────────────────────────────
async def generate_formatted_title(pages: list, total_pages: int, num: int, original_filename: str) -> str:
    """
    Claude로 [번호] 문서종류 ／ 제출자 ／ 핵심내용 ／ 사건번호 (pp.1-N).pdf 형식 생성.
    텍스트 없는 스캔 PDF는 원본 파일명에 번호만 붙여 반환.
    """
    base = original_filename.rsplit('.', 1)[0]  # .pdf 제거

    if not pages:
        return f"[{num:02d}] {base} (pp.1-{total_pages}).pdf"

    preview = "\n\n".join(p["text"] for p in pages[:6])[:3500]

    prompt = (
        "다음 법률 문서를 분석하여 아래 형식에 맞는 파일명을 출력하세요.\n\n"
        "형식 예시 (실제 플랫폼에서 사용 중):\n"
        "[01] 소장 ／ 원고 ／ 손해배상 청구 (토양오염·악취) ／ 2023가단62004 (pp.1-12).pdf\n"
        "[42] 감정서 핵심발췌 ／ 감정인 김한승 ／ 중금속검사 필요·토목공사비 누락 ／ 2023가단62004 (pp.1-38).pdf\n"
        "[46] 항소기록 접수통지서 ／ 광주고등법원 ／ 피고 항소인 김광영·박봉규·2026.5.13 수령 ／ 2026나20290 (pp.1-7).pdf\n"
        "[48] 금전공탁 통지서 ／ 공탁자 문영국 (피공탁자 정상문) ／ 141,033,382원 변제공탁 ／ 2026금1026 (pp.1-4).pdf\n\n"
        "출력 규칙:\n"
        f"- 번호: [{num:02d}] (고정, 변경 금지)\n"
        "- 구분자: \" ／ \" (전각 슬래시 U+FF0F, 앞뒤 공백 포함) — 반드시 이 문자 사용\n"
        "- 항목 4개: 문서종류 ／ 제출자 ／ 핵심내용 ／ 사건번호\n"
        f"- 마지막: \" (pp.1-{total_pages}).pdf\" (고정)\n"
        "- 핵심내용: 30자 이내, · 로 항목 연결\n"
        "- 사건번호: 문서에 기재된 사건번호 (예: 2023가단62004, 2026나20290)\n"
        "  사건번호가 없으면 핵심 키워드로 대체\n"
        "- 총 파일명 120자 이내\n"
        "- 파일명 한 줄만 출력. 설명·마크다운·따옴표 없이.\n\n"
        f"문서 내용:\n{preview}"
    )

    try:
        msg = await ai.messages.create(
            model=SUMMARY_MODEL,
            max_tokens=160,
            messages=[{"role": "user", "content": prompt}],
        )
        title = msg.content[0].text.strip().strip('"').strip("'")
        if re.match(r'^\[\d+\]', title):
            if not title.lower().endswith('.pdf'):
                title += '.pdf'
            return title[:220]
    except Exception:
        pass

    # 폴백: 번호만 붙임
    return f"[{num:02d}] {base} (pp.1-{total_pages}).pdf"


# ── AI 요약 (업로드 시 1회) ───────────────────────────────────────────
async def generate_summary(pages: list) -> str:
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
app = FastAPI(title="법률 문서 플랫폼", version="4.0")
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
            "star":           row.get("star", 0),
            "comment":        row.get("comment", ""),
            "lawyer_comment": row.get("lawyer_comment", ""),
            "summary":        row.get("summary", ""),
            "updated_at":     row.get("updated_at", 0),
        })
    return sorted(result, key=lambda x: x["updated_at"], reverse=True)

# ── POST /api/upload ──────────────────────────────────────────────────
ALLOWED_EXT = (".pdf", ".hwp", ".hwpx")

@app.post("/api/upload")
async def upload(file: UploadFile = File(...)):
    fname_lower = file.filename.lower()
    matched     = next((e for e in ALLOWED_EXT if fname_lower.endswith(e)), None)
    if not matched:
        raise HTTPException(400, "PDF 또는 HWP 파일만 업로드 가능합니다.")
    ext = matched.lstrip(".")  # 'pdf' / 'hwp' / 'hwpx'

    data = await file.read()
    fid  = hashlib.md5(file.filename.encode()).hexdigest()
    meta = load_meta()

    if fid in meta:
        return {"skipped": True, "file": meta[fid], "message": "이미 처리된 파일입니다."}

    num = next_doc_number(meta)
    (UPLOAD_DIR / f"{fid}.{ext}").write_bytes(data)

    if ext == "pdf":
        pages, is_scanned = extract_pages(data)
        total_pages       = _page_count(data)
        # 제목 생성 + AI 요약 병렬 실행
        if pages:
            formatted_title, summary = await asyncio.gather(
                generate_formatted_title(pages, total_pages, num, file.filename),
                generate_summary(pages),
            )
        else:
            summary         = "이미지 스캔 PDF — 텍스트 검색 불가, 열람만 가능합니다."
            formatted_title = await generate_formatted_title([], total_pages, num, file.filename)
    else:
        # HWP/HWPX: 텍스트 추출·AI 제목/요약 생략, 원본 파일명에 번호만 부여
        is_scanned      = False
        total_pages     = 0
        base            = file.filename.rsplit(".", 1)[0]
        formatted_title = f"[{num:02d}] {base}.{ext}"
        summary         = "HWP 문서 — 브라우저 미리보기 미지원. 다운로드하여 열람하세요."

    info = {
        "file_id":     fid,
        "file_name":   formatted_title,
        "total_pages": total_pages,
        "is_scanned":  is_scanned,
        "ext":         ext,
    }
    meta[fid] = info
    save_meta(meta)
    db_upsert(fid, summary=summary)

    return {"skipped": False, "file": info, "message": f"업로드 완료 — '{formatted_title}'"}

# ── GET /api/files/{fid}/pdf ──────────────────────────────────────────
@app.get("/api/files/{fid}/pdf")
def serve_pdf(fid: str, dl: bool = Query(False)):
    meta = load_meta()
    if fid not in meta:
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    ext      = meta[fid].get("ext", "pdf")
    file_path = UPLOAD_DIR / f"{fid}.{ext}"
    if not file_path.exists():
        raise HTTPException(404, "파일이 서버에 없습니다. 다시 업로드해 주세요.")
    fname     = meta[fid]["file_name"]
    media     = "application/pdf" if ext == "pdf" else "application/octet-stream"
    # HWP 등 비-PDF는 브라우저 인라인 렌더링 불가 → 항상 다운로드
    disposition = "attachment" if (dl or ext != "pdf") else "inline"
    return FileResponse(
        file_path,
        media_type=media,
        headers={"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(fname)}"},
    )

# ── DELETE /api/files/{fid} ───────────────────────────────────────────
@app.delete("/api/files/{fid}")
def delete_file(fid: str):
    meta = load_meta()
    if fid not in meta:
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    ext      = meta[fid].get("ext", "pdf")
    file_path = UPLOAD_DIR / f"{fid}.{ext}"
    if file_path.exists():
        file_path.unlink()
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
    meta = load_meta()
    if fid not in meta:
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    ext = meta[fid].get("ext", "pdf")
    if not new_name.lower().endswith((".pdf", ".hwp", ".hwpx")):
        new_name += f".{ext}"
    meta[fid]["file_name"] = new_name
    save_meta(meta)
    db_upsert(fid)  # updated_at 갱신
    return {"ok": True, "file_name": new_name}

# ── PATCH /api/files/{fid}/star ───────────────────────────────────────
class StarReq(BaseModel):
    star: int

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

# ── PATCH /api/files/{fid}/lawyer_comment ────────────────────────────
class LawyerCommentReq(BaseModel):
    lawyer_comment: str

@app.patch("/api/files/{fid}/lawyer_comment")
def patch_lawyer_comment(fid: str, req: LawyerCommentReq):
    if fid not in load_meta():
        raise HTTPException(404, "파일을 찾을 수 없습니다.")
    db_upsert(fid, lawyer_comment=req.lawyer_comment[:200])
    return {"ok": True, "lawyer_comment": req.lawyer_comment[:200]}

# ── 프론트엔드 정적 파일 서빙 ─────────────────────────────────────────
for _dist in [Path("./frontend_dist"), Path("../frontend/dist")]:
    if _dist.exists():
        app.mount("/", StaticFiles(directory=str(_dist), html=True), name="static")
        break

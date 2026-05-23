"""
법률 문서 검색 플랫폼 — 백엔드 v4
FastAPI + Anthropic Claude (AI 요약) + SQLite (별점·메모·요약 영속 저장)
벡터 검색 제거 — 파일명 기반 필터링만 사용
"""
import os, io, json, hashlib, sqlite3, time
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
    (UPLOAD_DIR / f"{fid}.pdf").write_bytes(data)

    summary = (
        (await generate_summary(pages))
        if pages
        else "이미지 스캔 PDF — 텍스트 검색 불가, 열람만 가능합니다."
    )

    info = {
        "file_id":     fid,
        "file_name":   file.filename,
        "total_pages": _page_count(data),
        "is_scanned":  is_scanned,
    }
    meta[fid] = info
    save_meta(meta)
    db_upsert(fid, summary=summary)

    return {"skipped": False, "file": info, "message": "업로드 완료"}

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

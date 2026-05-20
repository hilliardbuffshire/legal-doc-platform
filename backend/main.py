"""
법률 문서 검색 플랫폼 — 백엔드
FastAPI + ONNX 임베딩 (한국어) + Anthropic Claude
"""
import os, io, json, hashlib, pickle, asyncio, warnings
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
from pypdf import PdfReader
import numpy as np
import anthropic

load_dotenv()

# ── 설정 ──────────────────────────────────────────────────────────────
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CHAT_MODEL    = os.getenv("CLAUDE_MODEL", "claude-opus-4-5")
CHUNK_SIZE    = 700
CHUNK_OVR     = 80
TOP_K         = 8

ai       = anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY or "placeholder")
_pool    = ThreadPoolExecutor(max_workers=2)

# ── 영속 저장소 ───────────────────────────────────────────────────────
DATA_DIR   = Path(os.getenv("DATA_DIR", "."))
UPLOAD_DIR = DATA_DIR / "uploads"
META       = DATA_DIR / "meta.json"
STORE      = DATA_DIR / "vector_store.pkl"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

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
def extract_pages(pdf_bytes: bytes) -> list[dict]:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return [
        {"page": i + 1, "text": (p.extract_text() or "").strip()}
        for i, p in enumerate(reader.pages)
        if (p.extract_text() or "").strip()
    ]

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

# ── FastAPI ───────────────────────────────────────────────────────────
app = FastAPI(title="법률 문서 플랫폼", version="2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

# ── GET /api/files ─────────────────────────────────────────────────────
@app.get("/api/files")
def get_files():
    return sorted(load_meta().values(), key=lambda x: x["file_name"])

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

    pages = extract_pages(data)
    if not pages:
        raise HTTPException(400, "텍스트를 추출할 수 없습니다. (이미지 스캔 PDF)")

    (UPLOAD_DIR / f"{fid}.pdf").write_bytes(data)

    all_chunks = []
    for p in pages:
        all_chunks.extend(make_chunks(fid, file.filename, p["page"], p["text"]))

    store = load_store()
    existing_ids = {c["id"] for c in store}
    for c in all_chunks:
        if c["id"] not in existing_ids:
            c["embedding"] = await embed(c["text"])
            store.append(c)
    save_store(store)

    info = {
        "file_id":     fid,
        "file_name":   file.filename,
        "total_pages": len(pages),
        "chunks":      len(all_chunks),
    }
    meta[fid] = info
    save_meta(meta)
    return {"skipped": False, "file": info, "message": f"업로드 완료 ({len(all_chunks)}개 청크)"}

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
    return {"ok": True}

# ── GET /api/search ───────────────────────────────────────────────────
@app.get("/api/search")
async def search(q: str = Query(..., min_length=1)):
    store = load_store()
    if not store:
        return {"query": q, "results": []}

    hits_raw = cosine_search(store, await embed(q), n=min(20, len(store)))

    hits, seen = [], set()
    for h in hits_raw:
        m   = h["chunk"]["meta"]
        key = f"{m['file_name']}|{m['page']}"
        if key in seen:
            continue
        seen.add(key)
        hits.append({
            "text":      h["chunk"]["text"],
            "file_name": m["file_name"],
            "file_id":   m["file_id"],
            "page":      m["page"],
            "score":     round(h["score"], 3),
        })
        if len(hits) >= 10:
            break

    return {"query": q, "results": hits}

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

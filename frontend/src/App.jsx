import React, { useState, useEffect, useRef } from 'react'
import { getFiles, uploadFile, deleteFile, getPdfUrl, search, streamChat } from './api'

// 파일명에서 번호·종류·제출자 파싱
// 형식: [번호] 문서종류 ／ 제출자 ／ 핵심내용 ／ 사건번호 (pp.시작-끝).pdf
function parseMeta(fileName) {
  const m = fileName.match(/^\[(\d+)\]\s*([^／\n]+?)(?:\s*／\s*([^／\n]+?))?(?:\s*／|$)/)
  return {
    num:     m ? m[1] : null,
    docType: m ? m[2].trim() : null,
    sender:  m ? (m[3] || '').trim() : null,
  }
}

export default function App() {
  const [files,     setFiles]     = useState([])
  const [query,     setQuery]     = useState('')
  const [aiText,    setAiText]    = useState('')
  const [aiSources, setAiSources] = useState([])
  const [aiLoading, setAiLoading] = useState(false)
  const [filterType,   setFilterType]   = useState('')
  const [filterSender, setFilterSender] = useState('')
  const [uploading,    setUploading]    = useState(false)
  const [uploadMsg,    setUploadMsg]    = useState('')
  const fileInput = useRef(null)

  useEffect(() => { loadFiles() }, [])

  async function loadFiles() {
    try { setFiles(await getFiles()) } catch { /* silent */ }
  }

  // 검색 + AI 동시 실행
  async function handleSearch(e) {
    e.preventDefault()
    if (!query.trim()) return
    setAiText('')
    setAiSources([])
    setAiLoading(true)
    try {
      let buf = ''
      for await (const ev of streamChat(query)) {
        if (ev.type === 'text')    { buf += ev.content; setAiText(buf) }
        if (ev.type === 'sources') setAiSources(ev.sources)
        if (ev.type === 'done')    break
      }
    } catch (err) {
      setAiText('오류: ' + err.message)
    } finally {
      setAiLoading(false)
    }
  }

  async function handleUpload(e) {
    const selectedFiles = Array.from(e.target.files)
    if (!selectedFiles.length) return
    setUploading(true)
    setUploadMsg('')
    let ok = 0, skip = 0, fail = 0
    for (const f of selectedFiles) {
      try {
        const res = await uploadFile(f)
        res.skipped ? skip++ : ok++
      } catch {
        fail++
      }
    }
    setUploading(false)
    setUploadMsg(`완료: ${ok}개 업로드, ${skip}개 중복, ${fail}개 실패`)
    await loadFiles()
    e.target.value = ''
  }

  async function handleDelete(fid, name) {
    if (!confirm(`"${name}" 을(를) 삭제하시겠습니까?`)) return
    try {
      await deleteFile(fid)
      setFiles(f => f.filter(x => x.file_id !== fid))
    } catch (err) {
      alert('삭제 실패: ' + err.message)
    }
  }

  // 필터 옵션
  const allMeta   = files.map(f => ({ ...f, ...parseMeta(f.file_name) }))
  const types     = [...new Set(allMeta.map(f => f.docType).filter(Boolean))].sort()
  const senders   = [...new Set(allMeta.map(f => f.sender).filter(Boolean))].sort()
  const visible   = allMeta.filter(f =>
    (!filterType   || f.docType === filterType) &&
    (!filterSender || f.sender  === filterSender)
  )

  return (
    <div className="min-h-screen bg-slate-50">
      {/* 헤더 */}
      <header className="bg-white border-b px-6 py-4 flex items-center justify-between">
        <h1 className="text-lg font-bold text-slate-800">⚖️ 법률 문서 플랫폼</h1>
        <div className="flex items-center gap-3">
          {uploading && <span className="text-sm text-blue-600 animate-pulse">업로드 중…</span>}
          {uploadMsg && <span className="text-sm text-slate-500">{uploadMsg}</span>}
          <button
            onClick={() => fileInput.current?.click()}
            disabled={uploading}
            className="px-4 py-2 bg-blue-600 text-white text-sm rounded-lg hover:bg-blue-700 disabled:opacity-50"
          >
            + PDF 업로드
          </button>
          <input ref={fileInput} type="file" accept=".pdf" multiple className="hidden" onChange={handleUpload} />
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-4 py-8 space-y-8">

        {/* 파일명 형식 안내 */}
        <section className="bg-slate-800 rounded-2xl px-5 py-4 text-slate-300 text-sm space-y-3">
          <p className="text-xs font-semibold text-slate-400 uppercase tracking-widest">파일명 형식 안내</p>
          <div className="font-mono text-xs bg-slate-900 rounded-lg px-4 py-3 text-slate-300 break-all leading-relaxed">
            <span className="text-blue-400 font-bold">[01]</span>
            {' '}
            <span className="text-emerald-400">소장</span>
            {' ／ '}
            <span className="text-amber-400">원고</span>
            {' ／ '}
            <span className="text-slate-300">손해배상청구</span>
            {' ／ '}
            <span className="text-slate-500">2023가단62004</span>
            {' '}
            <span className="text-slate-500">(pp.1-12).pdf</span>
          </div>
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs">
            <span><span className="text-blue-400 font-bold">[ ]</span> 문서 번호</span>
            <span><span className="text-emerald-400 font-bold">◼</span> 문서 종류</span>
            <span><span className="text-amber-400 font-bold">◼</span> 제출자</span>
            <span><span className="text-slate-300 font-bold">◼</span> 핵심 내용</span>
            <span><span className="text-slate-500 font-bold">◼</span> 사건번호 &amp; 페이지</span>
          </div>
        </section>

        {/* 검색창 */}
        <section>
          <form onSubmit={handleSearch} className="flex gap-2">
            <input
              type="text"
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="찾으시는 문서가 있으세요? 질문을 입력하세요…"
              className="flex-1 px-4 py-3 border rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-blue-400 bg-white shadow-sm"
            />
            <button
              type="submit"
              disabled={aiLoading || !query.trim()}
              className="px-6 py-3 bg-blue-600 text-white text-sm rounded-xl hover:bg-blue-700 disabled:opacity-50 shadow-sm"
            >
              {aiLoading ? '분석 중…' : '검색'}
            </button>
          </form>
        </section>

        {/* AI 응답 */}
        {(aiText || aiLoading) && (
          <section className="bg-white rounded-2xl border shadow-sm p-6 space-y-4">
            <div className="flex items-center gap-2 text-sm font-semibold text-slate-700">
              <span>🤖</span><span>AI 분석 결과</span>
              {aiLoading && <span className="text-blue-500 animate-pulse ml-2">생성 중…</span>}
            </div>
            <p className="text-sm text-slate-700 whitespace-pre-wrap leading-relaxed">{aiText}</p>
            {aiSources.length > 0 && (
              <div className="pt-2 border-t space-y-1">
                <p className="text-xs font-semibold text-slate-500">📎 참조 문서</p>
                {aiSources.map((s, i) => (
                  <a
                    key={i}
                    href={getPdfUrl(s.file_id)}
                    target="_blank"
                    rel="noreferrer"
                    className="block text-xs text-blue-600 hover:underline truncate"
                  >
                    {s.file_name} · {s.page}페이지
                  </a>
                ))}
              </div>
            )}
          </section>
        )}

        {/* 문서 목록 */}
        <section className="space-y-3">
          <div className="flex items-center justify-between flex-wrap gap-3">
            <h2 className="text-sm font-semibold text-slate-600">
              전체 문서 {visible.length}/{files.length}건
            </h2>
            <div className="flex gap-2 flex-wrap">
              <select
                value={filterType}
                onChange={e => setFilterType(e.target.value)}
                className="text-sm border rounded-lg px-3 py-1.5 bg-white focus:outline-none focus:ring-2 focus:ring-blue-400"
              >
                <option value="">종류 전체</option>
                {types.map(t => <option key={t} value={t}>{t}</option>)}
              </select>
              <select
                value={filterSender}
                onChange={e => setFilterSender(e.target.value)}
                className="text-sm border rounded-lg px-3 py-1.5 bg-white focus:outline-none focus:ring-2 focus:ring-blue-400"
              >
                <option value="">제출자 전체</option>
                {senders.map(s => <option key={s} value={s}>{s}</option>)}
              </select>
              {(filterType || filterSender) && (
                <button
                  onClick={() => { setFilterType(''); setFilterSender('') }}
                  className="text-sm text-slate-500 hover:text-slate-700 px-2"
                >
                  ✕ 초기화
                </button>
              )}
            </div>
          </div>

          {visible.length === 0 ? (
            <div className="text-center py-16 text-slate-400 text-sm">
              {files.length === 0 ? '업로드된 문서가 없습니다. PDF를 업로드해 주세요.' : '조건에 맞는 문서가 없습니다.'}
            </div>
          ) : (
            <ul className="space-y-2">
              {visible.map(f => (
                <li key={f.file_id} className="bg-white rounded-xl border shadow-sm px-4 py-3 flex items-start justify-between gap-4 hover:border-blue-300 transition">
                  <div className="min-w-0 flex-1">
                    {f.num && (
                      <span className="inline-block text-xs font-bold text-blue-700 bg-blue-50 border border-blue-200 rounded px-1.5 py-0.5 mr-2 mb-1">
                        #{f.num}
                      </span>
                    )}
                    {f.docType && (
                      <span className="inline-block text-xs text-slate-500 bg-slate-100 rounded px-1.5 py-0.5 mr-2 mb-1">
                        {f.docType}
                      </span>
                    )}
                    {f.sender && (
                      <span className="inline-block text-xs text-emerald-700 bg-emerald-50 border border-emerald-200 rounded px-1.5 py-0.5 mb-1">
                        {f.sender}
                      </span>
                    )}
                    <p className="text-sm text-slate-800 break-all leading-snug mt-1">{f.file_name}</p>
                    <p className="text-xs text-slate-400 mt-0.5">{f.total_pages}페이지 · {f.chunks}청크</p>
                  </div>
                  <div className="flex gap-2 shrink-0 mt-0.5">
                    <a
                      href={getPdfUrl(f.file_id)}
                      target="_blank"
                      rel="noreferrer"
                      className="text-xs px-3 py-1.5 bg-slate-100 hover:bg-slate-200 rounded-lg text-slate-700"
                    >
                      열기
                    </a>
                    <a
                      href={getPdfUrl(f.file_id, true)}
                      className="text-xs px-3 py-1.5 bg-slate-100 hover:bg-slate-200 rounded-lg text-slate-700"
                    >
                      다운
                    </a>
                    <button
                      onClick={() => handleDelete(f.file_id, f.file_name)}
                      className="text-xs px-3 py-1.5 bg-red-50 hover:bg-red-100 rounded-lg text-red-600"
                    >
                      삭제
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </main>
    </div>
  )
}

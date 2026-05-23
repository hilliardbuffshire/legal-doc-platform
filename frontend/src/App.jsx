import React, { useState, useEffect, useRef } from 'react'
import {
  getFiles, uploadFile, deleteFile,
  getPdfUrl, patchStar, patchComment, patchLawyerComment, patchName,
} from './api'

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

// ── 별점 컴포넌트 ─────────────────────────────────────────────────────
function StarRating({ value, onChange }) {
  const [hovered, setHovered] = useState(0)
  return (
    <div className="flex gap-0.5" onMouseLeave={() => setHovered(0)}>
      {[1, 2, 3, 4, 5].map(n => (
        <button
          key={n}
          onClick={() => onChange(value === n ? 0 : n)}
          onMouseEnter={() => setHovered(n)}
          className="text-xl leading-none transition-colors focus:outline-none"
          style={{ color: n <= (hovered || value) ? '#f59e0b' : '#d1d5db' }}
          title={`별점 ${n}점${value === n ? ' (클릭하면 취소)' : ''}`}
        >
          ★
        </button>
      ))}
      {value > 0 && (
        <span className="text-xs text-amber-600 ml-1 self-center font-semibold">{value}점</span>
      )}
    </div>
  )
}

// ── AI 요약 아코디언 ──────────────────────────────────────────────────
function SummaryAccordion({ summary, isOpen, onToggle }) {
  return (
    <div className="mt-2.5">
      <button
        onClick={onToggle}
        className="flex items-center gap-1.5 text-sm text-blue-600 hover:text-blue-800 transition-colors select-none"
      >
        <span
          className="inline-block transition-transform duration-200 text-xs"
          style={{ transform: isOpen ? 'rotate(90deg)' : 'rotate(0deg)' }}
        >
          ▶
        </span>
        <span className="font-medium">AI 요약 {isOpen ? '접기' : '보기'}</span>
      </button>
      <div className={`summary-body${isOpen ? ' open' : ''}`}>
        <div>
          <p className="mt-2 text-sm text-slate-700 leading-relaxed bg-blue-50 rounded-xl px-4 py-3 border border-blue-100">
            {summary || '요약 생성에 실패했습니다. 파일을 다시 업로드해 주세요.'}
          </p>
        </div>
      </div>
    </div>
  )
}

// ── 메인 앱 ──────────────────────────────────────────────────────────
export default function App() {
  const [files,                setFiles]                = useState([])
  const [displayIds,           setDisplayIds]           = useState([])
  const [query,                setQuery]                = useState('')
  const [filterType,           setFilterType]           = useState('')
  const [filterSender,         setFilterSender]         = useState('')
  const [filterMinStar,        setFilterMinStar]        = useState(0)
  const [sortBy,               setSortBy]               = useState('updated_at')
  const [openSummaries,        setOpenSummaries]        = useState(new Set())
  const [editingComment,       setEditingComment]       = useState(null)
  const [commentDraft,         setCommentDraft]         = useState('')
  const [editingLawyerComment, setEditingLawyerComment] = useState(null)
  const [lawyerCommentDraft,   setLawyerCommentDraft]   = useState('')
  const [editingName,          setEditingName]          = useState(null)
  const [nameDraft,            setNameDraft]            = useState('')
  const [uploading,            setUploading]            = useState(false)
  const [uploadMsg,            setUploadMsg]            = useState('')
  const [uploadFailed,         setUploadFailed]         = useState([])
  const fileInput          = useRef(null)
  const commentRef         = useRef(null)
  const lawyerCommentRef   = useRef(null)
  const nameRef            = useRef(null)

  useEffect(() => { loadFiles() }, [])

  useEffect(() => {
    if (editingComment && commentRef.current) commentRef.current.focus()
  }, [editingComment])

  useEffect(() => {
    if (editingLawyerComment && lawyerCommentRef.current) lawyerCommentRef.current.focus()
  }, [editingLawyerComment])

  useEffect(() => {
    if (editingName && nameRef.current) {
      nameRef.current.focus()
      nameRef.current.select()
    }
  }, [editingName])

  // ── 정렬 헬퍼 ────────────────────────────────────────────────────
  function computeSortedIds(fileList, sortByVal) {
    return fileList
      .map(f => ({ ...f, ...parseMeta(f.file_name) }))
      .sort((a, b) => {
        if (sortByVal === 'star_desc') return (b.star - a.star) || (b.updated_at - a.updated_at)
        if (sortByVal === 'star_asc')  return (a.star - b.star) || (b.updated_at - a.updated_at)
        if (sortByVal === 'num')       return (Number(a.num) || 999) - (Number(b.num) || 999)
        return b.updated_at - a.updated_at
      })
      .map(f => f.file_id)
  }

  async function loadFiles() {
    try {
      const newFiles = await getFiles()
      setFiles(newFiles)
      setDisplayIds(computeSortedIds(newFiles, sortBy))
    } catch { /* silent */ }
  }

  function handleSortChange(newSortBy) {
    setSortBy(newSortBy)
    setDisplayIds(computeSortedIds(files, newSortBy))
  }

  // ── 별점 (순서 불변) ──────────────────────────────────────────────
  async function handleStarClick(fid, n) {
    const cur  = files.find(f => f.file_id === fid)?.star ?? 0
    const next = cur === n ? 0 : n
    setFiles(fs => fs.map(f => f.file_id === fid ? { ...f, star: next } : f))
    try { await patchStar(fid, next) } catch { /* silent */ }
  }

  // ── 원고 메모 ─────────────────────────────────────────────────────
  async function handleCommentSave(fid) {
    const comment = commentDraft.trim()
    setEditingComment(null)
    setFiles(fs => fs.map(f =>
      f.file_id === fid ? { ...f, comment, updated_at: Date.now() / 1000 } : f
    ))
    try { await patchComment(fid, comment) } catch { /* silent */ }
  }

  function handleCommentKeyDown(e, fid) {
    if (e.key === 'Enter') { e.preventDefault(); handleCommentSave(fid) }
    if (e.key === 'Escape') { setEditingComment(null) }
  }

  // ── 변호사 메모 ───────────────────────────────────────────────────
  async function handleLawyerCommentSave(fid) {
    const lawyer_comment = lawyerCommentDraft.trim()
    setEditingLawyerComment(null)
    setFiles(fs => fs.map(f =>
      f.file_id === fid ? { ...f, lawyer_comment, updated_at: Date.now() / 1000 } : f
    ))
    try { await patchLawyerComment(fid, lawyer_comment) } catch { /* silent */ }
  }

  function handleLawyerCommentKeyDown(e, fid) {
    if (e.key === 'Enter') { e.preventDefault(); handleLawyerCommentSave(fid) }
    if (e.key === 'Escape') { setEditingLawyerComment(null) }
  }

  // ── 제목 저장 ─────────────────────────────────────────────────────
  async function handleNameSave(fid) {
    const name = nameDraft.trim()
    if (!name) { setEditingName(null); return }
    setEditingName(null)
    setFiles(fs => fs.map(f =>
      f.file_id === fid
        ? { ...f, file_name: name.endsWith('.pdf') ? name : name + '.pdf', updated_at: Date.now() / 1000 }
        : f
    ))
    try { await patchName(fid, name) } catch { await loadFiles() }
  }

  function handleNameKeyDown(e, fid) {
    if (e.key === 'Enter') { e.preventDefault(); handleNameSave(fid) }
    if (e.key === 'Escape') { setEditingName(null) }
  }

  // ── AI 요약 토글 ──────────────────────────────────────────────────
  function toggleSummary(fid) {
    setOpenSummaries(prev => {
      const next = new Set(prev)
      next.has(fid) ? next.delete(fid) : next.add(fid)
      return next
    })
  }

  // ── 업로드 ────────────────────────────────────────────────────────
  async function handleUpload(e) {
    const selectedFiles = Array.from(e.target.files)
    if (!selectedFiles.length) return
    setUploading(true); setUploadMsg(''); setUploadFailed([])
    let ok = 0, skip = 0
    const failed = []
    for (const f of selectedFiles) {
      try {
        const res = await uploadFile(f)
        res.skipped ? skip++ : ok++
      } catch (err) {
        failed.push({ name: f.name, reason: err.message })
      }
    }
    setUploading(false)
    setUploadFailed(failed)
    setUploadMsg(
      failed.length === 0
        ? `완료: ${ok}개 업로드, ${skip}개 중복`
        : `완료: ${ok}개 업로드, ${skip}개 중복, ${failed.length}개 실패`
    )
    await loadFiles()
    e.target.value = ''
  }

  async function handleDelete(fid, name) {
    if (!confirm(`"${name}" 을(를) 삭제하시겠습니까?`)) return
    try {
      await deleteFile(fid)
      setFiles(f => f.filter(x => x.file_id !== fid))
      setDisplayIds(ids => ids.filter(id => id !== fid))
    } catch (err) {
      alert('삭제 실패: ' + err.message)
    }
  }

  // ── 사이드바 → 카드 스크롤 ───────────────────────────────────────
  function scrollToCard(fid) {
    const el = document.getElementById(`fc-${fid}`)
    if (!el) return
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
    el.style.outline = '2px solid #3b82f6'
    setTimeout(() => { el.style.outline = '' }, 1500)
  }

  // ── 파생 데이터 ───────────────────────────────────────────────────
  const allMeta     = files.map(f => ({ ...f, ...parseMeta(f.file_name) }))
  const allMetaById = Object.fromEntries(allMeta.map(f => [f.file_id, f]))
  const types       = [...new Set(allMeta.map(f => f.docType).filter(Boolean))].sort()
  const senders     = [...new Set(allMeta.map(f => f.sender).filter(Boolean))].sort()

  // visible: displayIds 순서 고정 + 현재 필터 적용
  const visible = displayIds
    .map(id => allMetaById[id])
    .filter(f => f &&
      (!query.trim()   || f.file_name.toLowerCase().includes(query.trim().toLowerCase())) &&
      (!filterType     || f.docType === filterType) &&
      (!filterSender   || f.sender  === filterSender) &&
      (filterMinStar === 0 || f.star >= filterMinStar)
    )

  const sidebarList = [...allMeta].sort((a, b) => (Number(a.num) || 999) - (Number(b.num) || 999))
  const visibleIds  = new Set(visible.map(f => f.file_id))
  const hasFilter   = filterType || filterSender || filterMinStar > 0

  return (
    <div className="min-h-screen bg-slate-50">

      {/* ── 헤더 ── */}
      <header className="bg-white border-b sticky top-0 z-10 shadow-sm">
        <div className="px-6 py-4 flex items-center justify-between">
          <h1 className="text-xl font-bold text-slate-800 tracking-tight">⚖️ 정상문 항소심 서류 파인더✝</h1>
          <div className="flex items-center gap-3">
            {uploading && <span className="text-sm text-blue-600 animate-pulse">업로드 중…</span>}
            {uploadMsg && (
              <span className={`text-sm font-medium ${uploadFailed.length > 0 ? 'text-red-600' : 'text-slate-500'}`}>
                {uploadMsg}
              </span>
            )}
            <button
              onClick={() => fileInput.current?.click()}
              disabled={uploading}
              className="px-4 py-2 bg-blue-600 text-white text-sm font-semibold rounded-lg hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              + PDF 업로드
            </button>
            <input ref={fileInput} type="file" accept=".pdf" multiple className="hidden" onChange={handleUpload} />
          </div>
        </div>

        {uploadFailed.length > 0 && (
          <div className="border-t border-red-100 bg-red-50 px-6 py-3 space-y-1">
            <div className="flex items-center justify-between">
              <p className="text-sm font-semibold text-red-700">⚠️ 업로드 실패한 파일 ({uploadFailed.length}개)</p>
              <button onClick={() => setUploadFailed([])} className="text-xs text-red-400 hover:text-red-600 transition-colors">
                닫기 ✕
              </button>
            </div>
            <ul className="space-y-1">
              {uploadFailed.map((f, i) => (
                <li key={i} className="text-sm text-red-800">
                  <span className="font-medium break-all">{f.name}</span>
                  {f.reason && <span className="text-red-500 ml-2 text-xs">— {f.reason}</span>}
                </li>
              ))}
            </ul>
          </div>
        )}
      </header>

      {/* ── 사이드바 + 메인 ── */}
      <div className="flex">

        {/* ── 좌측 파일 네비게이션 (w-72 = 288px, 텍스트 크기 확대) ── */}
        <nav className="hidden lg:block w-72 shrink-0 border-r border-slate-200 bg-white">
          <div className="sticky top-[65px] overflow-y-auto" style={{ height: 'calc(100vh - 65px)' }}>
            <div className="px-4 pt-4 pb-3 border-b border-slate-100">
              <p className="text-sm font-bold text-slate-500 uppercase tracking-wide">
                전체 목록 ({sidebarList.length})
              </p>
            </div>
            <ul className="py-2">
              {sidebarList.map(f => (
                <li key={f.file_id}>
                  <button
                    onClick={() => scrollToCard(f.file_id)}
                    className={`w-full text-left px-3 py-2 flex items-start gap-2 hover:bg-blue-50 transition-colors rounded-lg mx-1 ${
                      visibleIds.has(f.file_id) ? '' : 'opacity-35'
                    }`}
                    style={{ width: 'calc(100% - 8px)' }}
                    title={f.file_name}
                  >
                    <span className="shrink-0 text-sm font-bold text-blue-600 leading-snug pt-0.5 min-w-[2.5rem]">
                      [{f.num ?? '?'}]
                    </span>
                    <span
                      className="text-sm text-slate-700 leading-snug flex-1 overflow-hidden"
                      style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}
                    >
                      {f.docType || f.file_name}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        </nav>

        {/* ── 메인 콘텐츠 ── */}
        <main className="flex-1 min-w-0 px-4 lg:px-6 py-8 space-y-8">

          {/* ── 파일명 형식 안내 ── */}
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

          {/* ── 검색창 (파일 탐색기 방식 — 실시간 제목 필터링) ── */}
          <section>
            <div className="relative">
              <span className="absolute left-4 top-1/2 -translate-y-1/2 text-slate-400 text-lg pointer-events-none">🔍</span>
              <input
                type="text"
                value={query}
                onChange={e => setQuery(e.target.value)}
                placeholder="제목 검색 (예: 준비서면) — 입력 즉시 목록 필터링"
                className="w-full pl-11 pr-4 py-3 border rounded-xl text-base focus:outline-none focus:ring-2 focus:ring-blue-400 bg-white shadow-sm"
              />
              {query && (
                <button
                  onClick={() => setQuery('')}
                  className="absolute right-4 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 transition-colors text-sm"
                >
                  ✕
                </button>
              )}
            </div>
          </section>

          {/* ── 문서 목록 ── */}
          <section className="space-y-3">
            <div className="flex items-center justify-between flex-wrap gap-3">
              <h2 className="font-semibold text-slate-600">
                전체 문서 {visible.length}/{files.length}건
              </h2>
              <div className="flex gap-2 flex-wrap items-center">
                <select
                  value={sortBy}
                  onChange={e => handleSortChange(e.target.value)}
                  className="text-sm border rounded-lg px-3 py-1.5 bg-white focus:outline-none focus:ring-2 focus:ring-blue-400"
                >
                  <option value="updated_at">최신 수정순</option>
                  <option value="star_desc">별점 높은순</option>
                  <option value="star_asc">별점 낮은순</option>
                  <option value="num">번호순</option>
                </select>

                <select
                  value={filterMinStar}
                  onChange={e => setFilterMinStar(Number(e.target.value))}
                  className="text-sm border rounded-lg px-3 py-1.5 bg-white focus:outline-none focus:ring-2 focus:ring-blue-400"
                >
                  <option value={0}>별점 전체</option>
                  <option value={1}>★ 1점 이상</option>
                  <option value={2}>★★ 2점 이상</option>
                  <option value={3}>★★★ 3점 이상</option>
                  <option value={4}>★★★★ 4점 이상</option>
                  <option value={5}>★★★★★ 5점만</option>
                </select>

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

                {hasFilter && (
                  <button
                    onClick={() => { setFilterType(''); setFilterSender(''); setFilterMinStar(0) }}
                    className="text-sm text-slate-500 hover:text-slate-700 px-2 transition-colors"
                  >
                    ✕ 초기화
                  </button>
                )}
              </div>
            </div>

            {visible.length === 0 ? (
              <div className="text-center py-16 text-slate-400">
                {files.length === 0
                  ? '업로드된 문서가 없습니다. PDF를 업로드해 주세요.'
                  : '조건에 맞는 문서가 없습니다.'}
              </div>
            ) : (
              <ul className="space-y-2">
                {visible.map(f => (
                  <li
                    key={f.file_id}
                    id={`fc-${f.file_id}`}
                    className="bg-white rounded-2xl border shadow-sm px-5 py-4 hover:border-blue-300 transition-colors"
                  >
                    {/* 윗줄: 배지 + 버튼 */}
                    <div className="flex items-start justify-between gap-4">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap gap-1.5 items-center mb-1.5">
                          {f.num && (
                            <span className="text-sm font-bold text-blue-700 bg-blue-50 border border-blue-200 rounded px-1.5 py-0.5">
                              #{f.num}
                            </span>
                          )}
                          {f.docType && (
                            <span className="text-sm text-slate-500 bg-slate-100 rounded px-1.5 py-0.5">
                              {f.docType}
                            </span>
                          )}
                          {f.sender && (
                            <span className="text-sm text-emerald-700 bg-emerald-50 border border-emerald-200 rounded px-1.5 py-0.5">
                              {f.sender}
                            </span>
                          )}
                        </div>

                        {editingName === f.file_id ? (
                          <input
                            ref={nameRef}
                            type="text"
                            value={nameDraft}
                            onChange={e => setNameDraft(e.target.value)}
                            onBlur={() => handleNameSave(f.file_id)}
                            onKeyDown={e => handleNameKeyDown(e, f.file_id)}
                            className="w-full text-sm border border-blue-400 rounded-lg px-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-blue-400 bg-white mt-1"
                          />
                        ) : (
                          <div className="flex items-start gap-1.5 group/name mt-1">
                            <p className="text-slate-800 break-all leading-snug flex-1">{f.file_name}</p>
                            <button
                              onClick={() => { setEditingName(f.file_id); setNameDraft(f.file_name) }}
                              className="opacity-0 group-hover/name:opacity-100 transition-opacity shrink-0 text-slate-400 hover:text-blue-600 text-base leading-snug mt-0.5"
                              title="제목 수정"
                            >
                              ✏️
                            </button>
                          </div>
                        )}

                        <p className="text-sm text-slate-400 mt-0.5">
                          {f.total_pages}페이지
                          {f.is_scanned && (
                            <span className="ml-2 text-xs text-orange-500 font-medium">📷 이미지 스캔</span>
                          )}
                        </p>
                      </div>

                      <div className="flex gap-2 shrink-0 mt-0.5">
                        <a
                          href={getPdfUrl(f.file_id)}
                          target="_blank" rel="noreferrer"
                          className="text-sm px-3 py-1.5 bg-slate-100 hover:bg-slate-200 rounded-lg text-slate-700 transition-colors"
                        >
                          열기
                        </a>
                        <a
                          href={getPdfUrl(f.file_id, true)}
                          className="text-sm px-3 py-1.5 bg-slate-100 hover:bg-slate-200 rounded-lg text-slate-700 transition-colors"
                        >
                          다운
                        </a>
                        <button
                          onClick={() => { setEditingName(f.file_id); setNameDraft(f.file_name) }}
                          className="text-sm px-3 py-1.5 bg-slate-100 hover:bg-blue-100 rounded-lg text-blue-600 transition-colors"
                        >
                          제목수정
                        </button>
                        <button
                          onClick={() => handleDelete(f.file_id, f.file_name)}
                          className="text-sm px-3 py-1.5 bg-red-50 hover:bg-red-100 rounded-lg text-red-600 transition-colors"
                        >
                          삭제
                        </button>
                      </div>
                    </div>

                    {/* 별점 */}
                    <div className="mt-3">
                      <StarRating
                        value={f.star}
                        onChange={n => handleStarClick(f.file_id, n)}
                      />
                    </div>

                    {/* 원고 메모 */}
                    <div className="mt-2">
                      <span className="text-xs font-bold text-amber-700 mb-1 inline-block">[원고 메모]</span>
                      {editingComment === f.file_id ? (
                        <input
                          ref={commentRef}
                          type="text"
                          value={commentDraft}
                          onChange={e => setCommentDraft(e.target.value)}
                          onBlur={() => handleCommentSave(f.file_id)}
                          onKeyDown={e => handleCommentKeyDown(e, f.file_id)}
                          placeholder="원고 메모 입력 (Enter 저장, Esc 취소)…"
                          maxLength={200}
                          className="w-full text-sm border border-amber-300 rounded-lg px-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-amber-300 bg-amber-50"
                        />
                      ) : (
                        <button
                          onClick={() => { setEditingComment(f.file_id); setCommentDraft(f.comment || '') }}
                          className="text-left w-full group block"
                        >
                          {f.comment ? (
                            <span className="text-sm text-slate-700 bg-amber-50 border border-amber-200 rounded-lg px-3 py-1.5 inline-block group-hover:bg-amber-100 transition-colors">
                              📝 {f.comment}
                            </span>
                          ) : (
                            <span className="text-sm text-slate-400 group-hover:text-amber-600 transition-colors italic">
                              + 원고 메모 추가…
                            </span>
                          )}
                        </button>
                      )}
                    </div>

                    {/* 변호사 메모 */}
                    <div className="mt-2">
                      <span className="text-xs font-bold text-violet-700 mb-1 inline-block">[변호사 메모]</span>
                      {editingLawyerComment === f.file_id ? (
                        <input
                          ref={lawyerCommentRef}
                          type="text"
                          value={lawyerCommentDraft}
                          onChange={e => setLawyerCommentDraft(e.target.value)}
                          onBlur={() => handleLawyerCommentSave(f.file_id)}
                          onKeyDown={e => handleLawyerCommentKeyDown(e, f.file_id)}
                          placeholder="변호사 메모 입력 (Enter 저장, Esc 취소)…"
                          maxLength={200}
                          className="w-full text-sm border border-violet-300 rounded-lg px-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-violet-300 bg-violet-50"
                        />
                      ) : (
                        <button
                          onClick={() => { setEditingLawyerComment(f.file_id); setLawyerCommentDraft(f.lawyer_comment || '') }}
                          className="text-left w-full group block"
                        >
                          {f.lawyer_comment ? (
                            <span className="text-sm text-slate-700 bg-violet-50 border border-violet-200 rounded-lg px-3 py-1.5 inline-block group-hover:bg-violet-100 transition-colors">
                              ⚖️ {f.lawyer_comment}
                            </span>
                          ) : (
                            <span className="text-sm text-slate-400 group-hover:text-violet-600 transition-colors italic">
                              + 변호사 메모 추가…
                            </span>
                          )}
                        </button>
                      )}
                    </div>

                    {/* AI 요약 아코디언 */}
                    <SummaryAccordion
                      summary={f.summary}
                      isOpen={openSummaries.has(f.file_id)}
                      onToggle={() => toggleSummary(f.file_id)}
                    />
                  </li>
                ))}
              </ul>
            )}
          </section>
        </main>
      </div>
    </div>
  )
}

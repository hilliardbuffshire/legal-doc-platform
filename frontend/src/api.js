const BASE = ''

export function getPdfUrl(fid, download = false) {
  return `${BASE}/api/files/${fid}/pdf${download ? '?dl=true' : ''}`
}

export async function getFiles() {
  const r = await fetch(`${BASE}/api/files`)
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

export async function uploadFile(file) {
  const form = new FormData()
  form.append('file', file)
  const r = await fetch(`${BASE}/api/upload`, { method: 'POST', body: form })
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

export async function deleteFile(fid) {
  const r = await fetch(`${BASE}/api/files/${fid}`, { method: 'DELETE' })
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

export async function patchName(fid, name) {
  const r = await fetch(`${BASE}/api/files/${fid}/name`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

export async function patchStar(fid, star) {
  const r = await fetch(`${BASE}/api/files/${fid}/star`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ star }),
  })
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

export async function patchComment(fid, comment) {
  const r = await fetch(`${BASE}/api/files/${fid}/comment`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ comment }),
  })
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

export async function patchLawyerComment(fid, lawyer_comment) {
  const r = await fetch(`${BASE}/api/files/${fid}/lawyer_comment`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ lawyer_comment }),
  })
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

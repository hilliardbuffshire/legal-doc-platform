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

export async function search(q) {
  const r = await fetch(`${BASE}/api/search?q=${encodeURIComponent(q)}`)
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

export async function* streamChat(question) {
  const r = await fetch(`${BASE}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
  })
  if (!r.ok) throw new Error(await r.text())

  const reader = r.body.getReader()
  const dec    = new TextDecoder()
  let buf      = ''

  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    const lines = buf.split('\n')
    buf = lines.pop()
    for (const line of lines) {
      if (line.startsWith('data: ')) {
        const payload = line.slice(6).trim()
        if (payload) yield JSON.parse(payload)
      }
    }
  }
}

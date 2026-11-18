/* aicbbuu网页文件浏览 · 前端逻辑
   - 服务端渲染骨架，数据走 /api/list 局部刷新
   - 无第三方依赖 */
(() => {
'use strict';

const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));

function pathFromUrl() {
  try { return decodeURIComponent(location.pathname.replace(/^\/+|\/+$/g, '')); }
  catch (e) { return ''; }
}

const S = {
  path: pathFromUrl(),
  view: localStorage.getItem('rs_view') || 'list',
  sort: localStorage.getItem('rs_sort') || 'name',
  order: localStorage.getItem('rs_order') || 'asc',
  q: '',
  recursive: false,
  contentSearch: localStorage.getItem('rs_content') === '1',
  page: 1,
  entries: [],
  sel: new Set(),
  clip: (() => { try { return JSON.parse(sessionStorage.getItem('rs_clip') || 'null'); } catch (e) { return null; } })(),
};

/* ---------------- 工具 ---------------- */
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

const ICON_MAP = {
  dir: 'folder', image: 'image', video: 'video', audio: 'audio', pdf: 'pdf',
  doc: 'doc', sheet: 'sheet', slide: 'slide', zip: 'archive', code: 'code',
  web: 'web', text: 'text', script: 'script', data: 'data',
};
const ic = (name, cls) => `<svg class="ico${cls ? ' ' + cls : ''}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const ico = (kind, cls) => ic(ICON_MAP[kind] || 'file', cls);

function fmtSize(n) {
  if (n === null || n === undefined) return '—';
  if (n < 1024) return n + 'B';
  const u = ['K', 'M', 'G', 'T'];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < u.length - 1);
  return n.toFixed(1) + u[i];
}
function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = x => String(x).padStart(2, '0');
  const md = `${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  // 当年文件省掉年份，跨年才显示完整日期（省列宽、更好扫读）
  return d.getFullYear() === new Date().getFullYear() ? md : `${d.getFullYear()}-${md}`;
}
function fileUrl(rel, isDir) {
  return '/' + rel.split('/').map(encodeURIComponent).join('/') + (isDir ? '/' : '');
}
function thumbUrl(rel) {
  return '/thumb?w=320&path=' + encodeURIComponent(rel);
}
function joinRel(dir, name) { return dir ? dir + '/' + name : name; }

function toast(msg, type) {
  const t = document.createElement('div');
  t.className = 'toast ' + (type || 'success');
  t.textContent = msg;
  $('#toasts').appendChild(t);
  requestAnimationFrame(() => t.classList.add('show'));
  setTimeout(() => { t.classList.remove('show'); setTimeout(() => t.remove(), 2600); }, 2600);
}

async function api(url, method, data) {
  const opt = { method: method || 'GET', headers: {} };
  if (data instanceof FormData) opt.body = data;
  else if (data !== undefined) {
    opt.headers['Content-Type'] = 'application/json;charset=UTF-8';
    opt.body = JSON.stringify(data);
  }
  const r = await fetch(url, opt);
  let j = null;
  try { j = await r.json(); } catch (e) { j = null; }
  if (r.status === 401) { location.href = '/login'; throw new Error('登录已过期'); }
  if (!r.ok) throw new Error((j && j.error) || ('HTTP ' + r.status));
  return j;
}

/* 图片加载失败兜底：隐藏破图，显示占位图标 */
document.addEventListener('error', e => {
  const img = e.target;
  if (!img || img.tagName !== 'IMG') return;
  const holder = img.parentNode;
  if (!holder) return;
  img.style.visibility = 'hidden';
  if (!holder.querySelector('.imgfail')) {
    const s = document.createElement('span');
    s.className = 'imgfail';
    s.textContent = '🖼';
    holder.appendChild(s);
  }
}, true);

/* ---------------- 模态框 ---------------- */
let modalOnClose = null;
let lbEntry = null;
function openModal(title, bodyHtml, buttons, onClose) {
  modalOnClose = onClose || null;
  $('#modalTitle').textContent = title;
  $('#modalBody').innerHTML = bodyHtml;
  const foot = $('#modalFoot');
  foot.innerHTML = '';
  (buttons || []).forEach(b => {
    const btn = document.createElement('button');
    btn.className = 'btn ' + (b.cls || '');
    btn.textContent = b.text;
    btn.onclick = () => b.onClick && b.onClick();
    foot.appendChild(btn);
  });
  $('#modal').classList.add('show');
  const first = $('#modalBody input, #modalBody textarea');
  if (first) setTimeout(() => first.focus(), 60);
}
function closeModal() {
  $('#modal').classList.remove('show');
  if (modalOnClose) { const f = modalOnClose; modalOnClose = null; f(); }
}
$('#modalClose').onclick = closeModal;
$('#modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });

function prompt2(title, label, value, placeholder, onOk, okText) {
  openModal(title,
    `<label class="fl"><span>${esc(label)}</span><input id="pmtInput" value="${esc(value || '')}" placeholder="${esc(placeholder || '')}"></label>`,
    [
      { text: '取消', cls: '', onClick: closeModal },
      { text: okText || '确定', cls: 'primary', onClick: () => {
          const v = $('#pmtInput').value.trim();
          if (!v) return toast('请输入名称', 'warn');
          closeModal();
          onOk(v);
        } }
    ]);
  setTimeout(() => {
    const el = $('#pmtInput');
    if (!el) return;
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); $('#modalFoot .primary').click(); }
    });
  }, 80);
}

function confirmBox(title, html, okText, onOk, onCancel) {
  openModal(title, html, [
    { text: '取消', cls: '', onClick: () => { closeModal(); if (onCancel) onCancel(); } },
    { text: okText || '确定', cls: 'danger', onClick: () => { closeModal(); onOk(); } },
  ]);
}

/* ---------------- 载入与渲染 ---------------- */
async function load() {
  const area = $('#fileArea');
  // 目录与地址栏同步（刷新/分享可回到同一位置）
  try {
    const target = '/' + S.path.split('/').filter(Boolean).map(encodeURIComponent).join('/') + (S.path ? '/' : '');
    if (location.pathname !== target) history.replaceState(null, '', target);
  } catch (e) {}
  if (!S.entries.length) area.innerHTML = '<div class="empty"><div class="spin"></div><div>正在载入…</div></div>';
  const qs = new URLSearchParams({
    path: S.path, sort: S.sort, order: S.order, page: String(S.page),
  });
  if (S.q) {
    qs.set('q', S.q);
    if (S.recursive) {
      qs.set('recursive', '1');
      if (S.contentSearch) qs.set('content', '1');
    }
  }
  try {
    const d = await api('/api/list?' + qs.toString());
    S.entries = d.entries;
    S.total = d.total;
    renderCrumbs(d.crumbs, d.recursive);
    renderFiles(d);
    renderPager(d);
    renderStats(d.stats, d.disk);
    syncSel();
  } catch (e) {
    if (e.message.indexOf('Unauthorized') !== -1 || e.message.indexOf('401') !== -1) {
      location.href = '/login';
      return;
    }
    area.innerHTML = `<div class="empty"><div class="big">⚠</div><div>载入失败：${esc(e.message)}</div></div>`;
  }
}

function renderCrumbs(crumbs, recursive) {
  const parts = ['<a href="#" data-goto="">◈ 根目录</a>'];
  (crumbs || []).forEach(c => {
    parts.push('<span class="sep">/</span>');
    if (c.cur) parts.push(`<span class="cur">${esc(c.name)}</span>`);
    else parts.push(`<a href="#" data-goto="${esc(c.rel)}">${esc(c.name)}</a>`);
  });
  if (S.q) {
    const scope = recursive ? (S.contentSearch ? '全库内容' : '全库') : '本目录';
    parts.push(`<span class="sep">·</span><span class="cur" style="color:var(--cyan)">${scope}搜索 “${esc(S.q)}” · ${S.total} 条</span>`);
  }
  $('#crumbs').innerHTML = parts.join('');
}

function renderFiles(d) {
  const area = $('#fileArea');
  const rows = d.entries;
  if (!rows.length) {
    area.innerHTML = `<div class="empty"><div class="big">◦</div><div>${S.q ? '没有匹配的文件' : '此目录为空'}</div></div>`;
    return;
  }
  if (S.view === 'water') {
    renderWaterfall(rows);
  } else if (S.view === 'grid') {
    area.innerHTML = '<div class="grid">' + rows.map(cardHtml).join('') + '</div>';
  } else {
    area.innerHTML = '<div class="head-row">'
      + '<div></div><div></div>'
      + '<div class="h-sort" data-sort="name">名称</div>'
      + '<div class="h-sort r-size" data-sort="size">大小</div>'
      + '<div class="h-sort r-time" data-sort="time">修改时间</div>'
      + '<div style="text-align:right">操作</div></div>'
      + '<div class="rows">' + rows.map(rowHtml).join('') + '</div>';
  }
}

function visualHtml(e) {
  if (e.is_dir) return `<span class="k-${e.kind}">${ico(e.kind, 'big')}</span>`;
  if (e.kind === 'image') {
    if (e.ext === 'svg') return `<img src="${esc(fileUrl(e.rel, false))}" alt="" loading="lazy">`;
    return `<img src="${esc(thumbUrl(e.rel))}" alt="" loading="lazy">`;
  }
  if (e.kind === 'video') {
    return `<img src="${esc(thumbUrl(e.rel))}" alt="" loading="lazy"><span class="play">${ic('play')}</span>`;
  }
  return `<span class="k-${e.kind}">${ico(e.kind, 'big')}</span>`;
}

/* 瀑布流用原比例排版：图片按 dim 预留高度，避免加载后跳版 */
function visualStyle(e) {
  if (S.view !== 'water') return '';
  if (e.kind === 'image' && e.dim && e.dim[0] > 0) {
    return ` style="aspect-ratio:${e.dim[0]}/${e.dim[1]}"`;
  }
  if (e.kind === 'video') return ' style="aspect-ratio:16/9"';
  return '';
}

function rowHtml(e) {
  const sel = S.sel.has(e.rel) ? ' sel' : '';
  const isImg = !e.is_dir && e.kind === 'image';
  const thumb = isImg || (!e.is_dir && e.kind === 'video')
    ? `<img class="r-thumb" src="${esc(isImg && e.ext === 'svg' ? fileUrl(e.rel, false) : thumbUrl(e.rel))}" alt="" loading="lazy">`
    : `<span class="k-${e.kind}">${ico(e.kind)}</span>`;
  return `<div class="row${sel}" data-rel="${esc(e.rel)}">
    <div class="r-chk"><input type="checkbox" class="chk" ${sel ? 'checked' : ''}></div>
    <div class="r-ico">${thumb}</div>
    <div class="r-name${e.is_dir ? ' isdir' : ''}"><a href="#" data-open>${esc(e.name)}</a>${e.is_dir ? '/' : ''}${e.hit ? `<span class="hit" title="第 ${e.hit.line} 行：${esc(e.hit.text)}">内容 ${e.hit.line}</span>` : ''}</div>
    <div class="r-size">${e.is_dir
      ? `<span style="color:var(--faint)"${e.dir_size && e.dir_size.truncated ? ' title="超过统计上限，仅显示已统计部分"' : ''}>${e.children != null ? e.children + " 项" : "—"}${e.dir_size ? ` · ${e.dir_size.truncated ? '≥' : ''}${fmtSize(e.dir_size.bytes)}` : ''}</span>`
      : fmtSize(e.size)}</div>
    <div class="r-time">${fmtTime(e.mtime)}</div>
    <div class="r-act">
      ${e.is_dir ? `<button class="mini" data-act="open">${ic('right')}打开</button>` : `<button class="mini" data-act="preview">${ic('search')}预览</button>`}
      ${e.editable ? `<button class="mini" data-act="edit">${ic('edit')}编辑</button>` : ''}
      <button class="mini" data-act="link" title="复制直达路径链接">${ic('link')}路径链接</button>
      <button class="mini" data-act="more" title="更多">${ic('more')}</button>
    </div>
  </div>`;
}

function cardHtml(e) {
  const sel = S.sel.has(e.rel) ? ' sel' : '';
  return `<div class="card${sel}" data-rel="${esc(e.rel)}">
    <input type="checkbox" class="c-chk chk" ${sel ? 'checked' : ''}>
    <button class="c-more" data-act="more" title="更多操作"><svg class="ico sm"><use href="#i-more"/></svg></button>
    <div class="c-visual"${visualStyle(e)}>${visualHtml(e)}</div>
    <div class="c-name${e.is_dir ? ' isdir' : ''}"><a href="#" data-open title="${esc(e.name)}">${esc(e.name)}</a></div>
    <div class="c-meta"><span>${e.is_dir ? (e.children != null ? e.children + ' 项' : '文件夹') : fmtSize(e.size)}</span><span>${e.hit ? '内容命中' : fmtTime(e.mtime).slice(5, 16)}</span></div>
  </div>`;
}

/* 瀑布流：按列分发，每次放进当前最矮的一列（图片高度未知时用估算） */
function renderWaterfall(rows) {
  const area = $('#fileArea');
  const w = area.clientWidth || 1200;
  const colW = 210, gap = 12;
  const cols = Math.max(1, Math.min(8, Math.floor((w - gap) / (colW + gap))));
  const heights = new Array(cols).fill(0);
  const buckets = Array.from({length: cols}, () => []);
  rows.forEach(e => {
    let k = 0;
    for (let i = 1; i < cols; i++) if (heights[i] < heights[k]) k = i;
    buckets[k].push(e);
    let vis = 130;
    if (e.kind === 'image' && e.dim && e.dim[0] > 0) vis = Math.round(colW * e.dim[1] / e.dim[0]);
    else if (e.kind === 'video') vis = Math.round(colW * 9 / 16);
    heights[k] += Math.min(vis, 420) + 54;
  });
  area.innerHTML = '<div class="water">' + buckets
    .filter(c => c.length)
    .map(c => '<div class="wcol">' + c.map(cardHtml).join('') + '</div>').join('') + '</div>';
}

function renderPager(d) {
  const box = $('#pager');
  if (!d.total_pages || d.total_pages <= 1) { box.innerHTML = ''; return; }
  box.innerHTML = `
    <button id="pgPrev" ${d.page <= 1 ? 'disabled' : ''}>« 上一页</button>
    <span class="pg-info">${d.page} / ${d.total_pages} · 共 ${d.total} 项</span>
    <button id="pgNext" ${d.page >= d.total_pages ? 'disabled' : ''}>下一页 »</button>`;
  $('#pgPrev').onclick = () => { if (d.page > 1) { S.page = d.page - 1; S.sel.clear(); load(); window.scrollTo({top: 0, behavior: 'smooth'}); } };
  $('#pgNext').onclick = () => { if (d.page < d.total_pages) { S.page = d.page + 1; S.sel.clear(); load(); window.scrollTo({top: 0, behavior: 'smooth'}); } };
}

function renderOverview(stats) {
  const el = $('#overview');
  if (!el) return;
  if (!stats) { el.innerHTML = ''; return; }
  el.innerHTML = [
    ['文件', String(stats.files)],
    ['总体积', fmtSize(stats.bytes)],
    ['图片', String(stats.images || 0)],
    ['今日更新', String(stats.today || 0)],
  ].map(([l, v]) => `<div class="ov-item"><span class="v">${v}</span><span class="l">${l}</span></div>`).join('');
}

function renderStats(stats, disk) {
  const shown = S.entries.length;
  $('#stCount').textContent = `本目录 ${S.total} 项 · 显示 ${shown}`;
  renderOverview(stats);
  if (disk) {
    const ring = $('#diskRing');
    ring.style.setProperty('--p', disk.percent);
    ring.className = 'ring' + (disk.percent >= 90 ? ' crit' : disk.percent >= 75 ? ' hot' : '');
    $('#diskText').textContent = `磁盘 已用 ${disk.percent}% · 剩余 ${fmtSize(disk.free)}`;
  }
}

/* ---------------- 选择 ---------------- */
function syncSel() {
  // 从 DOM 反向同步（视图切换后）
  const bar = $('#selBar');
  const n = S.sel.size;
  $$('.row, .card').forEach(el => {
    const rel = el.dataset.rel;
    if (!rel) return;
    const on = S.sel.has(rel);
    el.classList.toggle('sel', on);
    const cb = el.querySelector('.chk');
    if (cb) cb.checked = on;
  });
  if (n === 0 && !S.clip) { bar.classList.remove('show'); bar.innerHTML = ''; return; }
  let html = n ? `<span class="cnt">已选 ${n} 项</span>` : '<span class="cnt" style="color:var(--dim)">剪贴板</span>';
  if (n) {
    html += `<button class="mini" data-bulk="download">${ic('download')}下载</button>`;
    html += `<button class="mini" data-bulk="copy">${ic('file')}复制</button>`;
    html += `<button class="mini" data-bulk="cut">${ic('file')}剪切</button>`;
    if (n === 1) html += `<button class="mini" data-bulk="rename">${ic('edit')}重命名</button>`;
    html += `<button class="mini danger" data-bulk="delete">${ic('trash')}删除(移入回收站)</button>`;
    html += `<button class="mini" data-bulk="clear">${ic('close')}取消</button>`;
  }
  if (S.clip) {
    const mode = S.clip.cut ? '剪切' : '复制';
    html += `<span style="font-size:12px;color:var(--dim);padding:0 2px">待${mode} ${S.clip.paths.length} 项</span>`;
    html += `<button class="mini" data-bulk="paste">${ic('download')}粘贴到此处</button>`;
    html += `<button class="mini" data-bulk="unclip">${ic('close')}</button>`;
  }
  bar.innerHTML = html;
  bar.classList.add('show');
}

function saveClip() {
  try {
    if (S.clip) sessionStorage.setItem('rs_clip', JSON.stringify(S.clip));
    else sessionStorage.removeItem('rs_clip');
  } catch (e) {}
}

/* ---------------- 事件：文件区 ---------------- */
$('#fileArea').addEventListener('click', e => {
  const row = e.target.closest('.row, .card');
  if (!row) return;
  const entry = S.entries.find(x => x.rel === row.dataset.rel);
  if (!entry) return;

  // 复选框：只切换选中，不触发打开
  const chk = e.target.closest('.chk');
  if (chk) {
    if (chk.checked) S.sel.add(entry.rel); else S.sel.delete(entry.rel);
    syncSel();
    return;
  }

  // 行内动作按钮：必须用 closest —— 按钮里面是 <svg>，直接读 e.target.dataset.act
  // 会拿到 undefined（点到图标就没反应，点到按钮边缘才有反应）。
  const actEl = e.target.closest('[data-act]');
  if (actEl) {
    const act = actEl.dataset.act;
    if (act === 'open') return goInto(entry.rel);
    if (act === 'preview') return openLightbox(entry);
    if (act === 'edit') { location.href = '/edit?path=' + encodeURIComponent(entry.rel); return; }
    if (act === 'link') return copyPathLink(entry);
    if (act === 'more') return showActions(entry);
  }

  // 点在操作区的空隙（按钮之间 5px 的 gap）上，什么也不做
  if (e.target.closest('.r-act, .c-more')) return;

  // 其余位置（文件名、缩略图、行空白）：目录进下一层，文件开预览
  e.preventDefault();
  if (entry.is_dir) goInto(entry.rel); else openLightbox(entry);
});

/* URL 段编码：只转义会破坏 URL 结构的字符，中文保持可读原样
   （浏览器地址栏和 curl 都能直接访问中文路径） */
function urlSeg(s) {
  return String(s).replace(/[%#?&+ ]/g, c => '%' + c.charCodeAt(0).toString(16).toUpperCase());
}

/* 复制直达路径链接（就是地址栏那个地址） */
async function copyPathLink(entry) {
  const segs = entry.rel.split('/').filter(Boolean).map(urlSeg);
  const url = location.origin + '/' + segs.join('/') + (entry.is_dir ? '/' : '');
  try {
    await navigator.clipboard.writeText(url);
    toast('路径链接已复制：' + url);
  } catch (e) {
    // 非 HTTPS 或浏览器拒绝剪贴板时，退回可手选的输入框
    prompt2('路径链接（请手动复制）', '全选后 Ctrl+C', url, '', () => {});
  }
}

function goInto(rel) {
  S.path = rel; S.page = 1; S.q = ''; S.recursive = false; S.sel.clear();
  const si = $('#searchInput'); if (si) si.value = '';
  load();
}

$('#crumbs').addEventListener('click', e => {
  const a = e.target.closest('[data-goto]');
  if (!a) return;
  e.preventDefault();
  S.path = a.dataset.goto;
  S.page = 1; S.sel.clear(); S.q = ''; S.recursive = false;
  $('#searchInput').value = '';
  load();
});

$('#fileArea').addEventListener('click', e => {
  const h = e.target.closest('[data-sort]');
  if (!h) return;
  const f = h.dataset.sort;
  if (S.sort === f) S.order = S.order === 'asc' ? 'desc' : 'asc';
  else { S.sort = f; S.order = 'asc'; }
  localStorage.setItem('rs_sort', S.sort);
  localStorage.setItem('rs_order', S.order);
  $('#sortSelect').value = S.sort;
  $('#btnOrder').textContent = S.order === 'asc' ? '↑' : '↓';
  S.page = 1;
  load();
});

/* ---------------- 行内「更多」菜单 ---------------- */
function showActions(entry) {
  const kv = `<dl class="kv">
    <dt>名称</dt><dd>${esc(entry.name)}</dd>
    <dt>路径</dt><dd>${esc(entry.rel || entry.name)}</dd>
    <dt>类型</dt><dd>${esc(entry.is_dir ? '文件夹' : (entry.kind + (entry.ext ? ' · .' + entry.ext : '')))}</dd>
    <dt>大小</dt><dd>${entry.is_dir ? '—' : fmtSize(entry.size)}</dd>
    <dt>修改时间</dt><dd>${fmtTime(entry.mtime)}</dd>
  </dl>`;
  openModal('文件详情', kv, [
    { text: '关闭', cls: 'primary', onClick: closeModal },
    { text: '下载', cls: '', onClick: () => { closeModal(); doDownload([entry.rel]); } },
    { text: '分享', cls: '', onClick: () => { closeModal(); openShare(entry); } },
    { text: '重命名', cls: '', onClick: () => { closeModal(); doRename([entry.rel]); } },
    { text: '删除(移入回收站)', cls: 'danger', onClick: () => { closeModal(); doDelete([entry.rel]); } },
  ]);
}

/* ---------------- 批量操作条 ---------------- */
$('#selBar').addEventListener('click', e => {
  const b = e.target.closest('[data-bulk]');
  if (!b) return;
  const act = b.dataset.bulk;
  const paths = Array.from(S.sel);
  if (act === 'clear') { S.sel.clear(); syncSel(); return; }
  if (act === 'download') return doDownload(paths);
  if (act === 'delete') return doDelete(paths);
  if (act === 'rename') return doRename(paths);
  if (act === 'copy' || act === 'cut') {
    S.clip = { paths, cut: act === 'cut' };
    saveClip(); syncSel();
    toast(`已${act === 'copy' ? '复制' : '剪切'} ${paths.length} 项，到目标目录点「粘贴」`);
    return;
  }
  if (act === 'paste') return doPaste();
  if (act === 'unclip') { S.clip = null; saveClip(); syncSel(); return; }
});

async function doPaste() {
  if (!S.clip) return;
  const url = S.clip.cut ? '/move' : '/copy';
  try {
    const d = await api(url, 'POST', { paths: S.clip.paths, dest: S.path });
    if (S.clip.cut) { S.clip = null; saveClip(); }
    toast(d.msg || '完成');
    S.sel.clear();
    load();
  } catch (err) { toast('粘贴失败：' + err.message, 'error'); }
}

/* ---------------- 删除 / 重命名 / 下载 ---------------- */
/* 删除 = 移入回收站（可还原），所以不再弹二次确认 */
async function doDelete(paths) {
  if (!paths.length) return;
  try {
    const d = await api('/delete', 'POST', { paths });
    toast(d.msg || '已移入回收站');
    S.sel.clear(); S.page = 1; load();
  } catch (e) { toast('删除失败：' + e.message, 'error'); }
}

function doRename(paths) {
  if (!paths.length) return;
  const single = paths.length === 1;
  const old = single ? paths[0].split('/').pop() : '';
  prompt2(single ? '重命名' : `批量重命名 (${paths.length} 项)`,
    single ? '新名称（含扩展名）' : '基础名称（自动加序号，保留扩展名）',
    old, single ? '如 note.txt' : '如 image',
    async (name) => {
      if (single) {
        const parts = paths[0].split('/'); parts.pop(); parts.push(name);
        return renameOne(paths[0], parts.join('/'));
      }
      for (let i = 0; i < paths.length; i++) {
        const p = paths[i];
        const slash = p.lastIndexOf('/');
        const base = p.slice(slash + 1);
        const dot = base.lastIndexOf('.');
        const ext = dot > 0 ? base.slice(dot) : '';
        const dir = slash >= 0 ? p.slice(0, slash + 1) : '';
        const dst = dir + name + '_' + (i + 1) + ext;
        try { await renameOne(p, dst, true); } catch (e) {}
      }
      toast(`已重命名 ${paths.length} 项`);
      S.sel.clear(); load();
    });
}
async function renameOne(src, dst, silent) {
  if (dst === null) return; // 批量模式下占位
  try {
    const d = await api('/rename', 'POST', { src, dst });
    if (d.conflict) {
      confirmBox('目标已存在', `<p>目标已存在，是否覆盖？</p><p><code>${esc(dst)}</code></p>`, '覆盖',
        async () => {
          try { await api('/rename', 'POST', { src, dst, overwrite: true }); toast('已覆盖并重命名'); S.sel.clear(); load(); }
          catch (e) { toast('失败：' + e.message, 'error'); }
        });
      return;
    }
    if (!silent) { toast(d.msg || '已重命名'); S.sel.clear(); load(); }
  } catch (e) { if (!silent) toast('重命名失败：' + e.message, 'error'); throw e; }
}

async function doDownload(paths) {
  if (!paths.length) return;
  try {
    toast(`正在打包 ${paths.length} 项…`);
    const r = await fetch('/download', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ paths }),
    });
    if (!r.ok) { let m = 'HTTP ' + r.status; try { m = (await r.json()).error || m; } catch (e) {} throw new Error(m); }
    const dispo = r.headers.get('Content-Disposition') || '';
    let name = 'download';
    let m = dispo.match(/filename\*=UTF-8''([^;]+)/i);
    if (m) { try { name = decodeURIComponent(m[1]); } catch (e) {} }
    else { m = dispo.match(/filename="?([^";]+)"?/i); if (m) name = m[1]; }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = name; a.style.display = 'none';
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    toast(`已开始下载`);
  } catch (e) { toast('下载失败：' + e.message, 'error'); }
}

/* ---------------- 新建 ---------------- */
$('#btnNewFile').onclick = () => {
  prompt2('新建文件', '文件名（含扩展名）', '', '如 note.txt', async (name) => {
    try {
      const rel = joinRel(S.path, name);
      const d = await api('/edit', 'POST', { path: rel, content: '' });
      toast(d.msg || '已创建');
      load();
    } catch (e) { toast('创建失败：' + e.message, 'error'); }
  }, '创建');
};
$('#btnNewFolder').onclick = () => {
  prompt2('新建文件夹', '文件夹名', '', '如 images', async (name) => {
    try {
      const d = await api('/mkdir', 'POST', { dir: S.path, name });
      toast(d.msg || '已创建');
      load();
    } catch (e) { toast('创建失败：' + e.message, 'error'); }
  }, '创建');
};

/* ---------------- 上传 ---------------- */
let pendingFiles = [];

$('#btnUpload').onclick = () => $('#fileInput').click();
$('#fileInput').onchange = e => {
  const files = Array.from(e.target.files || []);
  if (!files.length) return;
  pendingFiles = files;
  $('#fileInput').value = '';
  askUpload(files);
};

function askUpload(files) {
  const names = files.slice(0, 8).map(f => esc(f.name) + '  <span style="color:var(--faint)">' + fmtSize(f.size) + '</span>').join('<br>');
  const more = files.length > 8 ? `<br>… 等共 ${files.length} 个文件` : '';
  const total = files.reduce((a, f) => a + f.size, 0);
  openModal('确认上传',
    `<p>目标目录：<code>/${esc(S.path || '')}</code></p>
     <p>共 <strong>${files.length}</strong> 个文件 · ${fmtSize(total)}</p>
     <p style="font-size:12px"><code>${names}${more}</code></p>`,
    [
      { text: '取消', cls: '', onClick: closeModal },
      { text: '开始上传', cls: 'primary', onClick: () => { closeModal(); doUpload(files, false); } },
    ]);
}

function doUpload(files, overwrite) {
  const form = new FormData();
  form.append('dir', S.path);
  files.forEach(f => form.append('file', f));
  if (overwrite) form.append('overwrite', '1');

  const bar = $('#uploadBar'), fill = $('#ubFill'), pct = $('#ubPct'), label = $('#ubName');
  bar.classList.add('show');
  fill.style.width = '0%'; pct.textContent = '0%';
  label.textContent = `上传 ${files.length} 个文件`;

  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/upload', true);
  xhr.upload.onprogress = e => {
    if (!e.lengthComputable) return;
    const p = Math.round(e.loaded * 100 / e.total);
    fill.style.width = p + '%'; pct.textContent = p + '%';
  };
  xhr.onload = () => {
    setTimeout(() => bar.classList.remove('show'), 400);
    let d = {};
    try { d = JSON.parse(xhr.responseText || '{}'); } catch (e) {}
    if (xhr.status < 200 || xhr.status >= 300) {
      return toast('上传失败：' + (d.error || ('HTTP ' + xhr.status)), 'error');
    }
    if (d.conflict && d.conflict.length) {
      const names = d.conflict;
      const conflictFiles = files.filter(f => names.indexOf(f.name) !== -1);
      confirmBox('存在同名文件',
        `<p>以下 ${names.length} 个文件已存在，是否覆盖？</p><p><code>${names.slice(0, 10).map(esc).join('<br>')}</code></p>`,
        '覆盖',
        () => {
          if (d.msg) toast(d.msg);
          doUpload(conflictFiles, true);
        });
      if (d.saved) load();
      return;
    }
    if (S.q) {                            // 搜索状态下上传，新文件会被过滤掉，先清搜索
      S.q = ''; S.recursive = false;
      const si = $('#searchInput'); if (si) si.value = '';
    }
    toast(d.msg || '上传完成');
    S.page = 1;
    load();
  };
  xhr.onerror = () => { bar.classList.remove('show'); toast('上传失败：网络错误', 'error'); };
  xhr.send(form);
}

/* 拖拽上传 */
let dragDepth = 0;
window.addEventListener('dragenter', e => {
  if (!e.dataTransfer || Array.from(e.dataTransfer.types || []).indexOf('Files') === -1) return;
  dragDepth++;
  $('#dropTip').classList.add('show');
});
window.addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) $('#dropTip').classList.remove('show');
});
window.addEventListener('dragover', e => e.preventDefault());
window.addEventListener('drop', e => {
  e.preventDefault();
  dragDepth = 0;
  $('#dropTip').classList.remove('show');
  const files = Array.from(e.dataTransfer.files || []);
  if (!files.length) return;
  askUpload(files);
});

/* ---------------- 灯箱预览 ---------------- */
const TEXT_KINDS = ['text', 'code', 'web', 'data', 'script'];
let lbHtmlSource = false;

/* HTML/HTM：默认渲染成网页（iframe），可切到源码 */
async function paintHtml(entry, url, source) {
  const body = $('#lbBody');
  if (!source) {
    body.innerHTML = `<iframe src="${esc(url)}" title="${esc(entry.name)}"></iframe>`;
    return;
  }
  body.innerHTML = '<div class="spin"></div>';
  try {
    const LIMIT = 400000;
    const big = entry.size > LIMIT;
    const r = await fetch(url, big ? { headers: { Range: 'bytes=0-' + (LIMIT - 1) } } : {});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const txt = await r.text();
    body.innerHTML = `<pre>${esc(txt)}</pre>`;
    if (big) toast('文件较大，仅显示前 400KB', 'warn');
  } catch (e) {
    body.innerHTML = `<div class="empty"><div class="big">⚠</div><div>读取失败：${esc(e.message)}</div></div>`;
  }
}

async function openLightbox(entry) {
  lbEntry = entry;
  lbHtmlSource = false;
  const url = fileUrl(entry.rel, false);
  const body = $('#lbBody');
  const isHtml = /\.(html?|xhtml)$/i.test(entry.name);
  $('#lbName').textContent = entry.name;
  $('#lbMeta').textContent = `${fmtSize(entry.size)} · ${fmtTime(entry.mtime)}`;
  $('#lbActions').innerHTML = `
    <a class="mini" href="${esc(url)}" download>${ic('download')}下载</a>
    ${isHtml ? `<button class="mini" data-lb="src">${ic('code')}<span id="srcLabel">源码</span></button>` : ''}
    ${entry.editable ? `<a class="mini" href="/edit?path=${encodeURIComponent(entry.rel)}">${ic('edit')}编辑</a>` : ''}
    <button class="mini" data-lb="reload" title="重新载入当前文件">${ic('refresh')}刷新</button>
    ${SHAREABLE.indexOf(entry.kind) !== -1 ? `<button class="mini" data-lb="share">${ic('share')}分享</button>` : ''}`;

  body.innerHTML = '<div class="spin"></div>';
  // 先亮出灯箱再填内容：文本/HTML 要走异步读取，不能等它回来才给反馈
  $('#lightbox').classList.add('show');
  document.body.style.overflow = 'hidden';

  if (entry.kind === 'image') {
    body.innerHTML = `<img src="${esc(url)}" alt="${esc(entry.name)}">`;
  } else if (entry.kind === 'video') {
    body.innerHTML = `<video src="${esc(url)}" controls autoplay playsinline></video>`;
  } else if (entry.kind === 'audio') {
    body.innerHTML = `<audio src="${esc(url)}" controls autoplay></audio>`;
  } else if (entry.kind === 'pdf') {
    body.innerHTML = `<iframe src="${esc(url)}"></iframe>`;
  } else if (isHtml) {
    // 网页默认渲染，不再一上来就甩一屏源码
    await paintHtml(entry, url, false);
  } else if (TEXT_KINDS.indexOf(entry.kind) !== -1 || entry.editable) {
    try {
      const LIMIT = 400000;
      const big = entry.size > LIMIT;
      const r = await fetch(url, big ? { headers: { Range: 'bytes=0-' + (LIMIT - 1) } } : {});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const txt = await r.text();
      body.innerHTML = `<pre>${esc(txt)}</pre>`;
      if (big) toast('文件较大，仅显示前 400KB', 'warn');
    } catch (e) {
      body.innerHTML = `<div class="empty"><div class="big">⚠</div><div>无法预览：${esc(e.message)}</div></div>`;
    }
  } else {
    body.innerHTML = `<div class="empty"><div class="big">${ico(entry.kind, 'big')}</div>
      <div>该类型不支持内嵌预览</div>
      <div><a class="btn primary" href="${esc(url)}" download>${ic('download')}下载文件</a></div></div>`;
  }
}
function closeLightbox() {
  $('#lightbox').classList.remove('show');
  $('#lbBody').innerHTML = '';
  document.body.style.overflow = '';
}
$('#lbActions').addEventListener('click', e => {
  const b = e.target.closest('[data-lb]');
  if (!b || !lbEntry) return;
  if (b.dataset.lb === 'share') return openShare(lbEntry);
  if (b.dataset.lb === 'reload') { lbHtmlSource = false; return openLightbox(lbEntry); }
  if (b.dataset.lb === 'src') {
    lbHtmlSource = !lbHtmlSource;
    const lbl = $('#srcLabel');
    if (lbl) lbl.textContent = lbHtmlSource ? '预览' : '源码';
    paintHtml(lbEntry, fileUrl(lbEntry.rel, false), lbHtmlSource);
  }
});
$('#btnTrash').onclick = openTrash;
$('#btnShares').onclick = openShares;
$('#lbClose').onclick = closeLightbox;
$('#lightbox').addEventListener('click', e => { if (e.target.id === 'lightbox') closeLightbox(); });

/* ---------------- 已分享列表 ---------------- */
async function openShares() {
  let d;
  try { d = await api('/api/shares'); }
  catch (e) { return toast('读取分享列表失败：' + e.message, 'error'); }
  const items = d.items || [];
  const now = Date.now() / 1000;
  const rows = items.map(it => {
    const left = Math.max(0, Math.ceil((it.expires - now) / 86400));
    return `
    <div class="tr-row">
      <span class="tr-ico k-doc">${ic('share', 'big')}</span>
      <span class="tr-main"><b>${esc(it.path || '（根目录）')}</b>
        <em title="${esc(it.url)}">${esc(it.url)}</em></span>
      <span class="tr-meta">还有 ${left} 天</span>
      <button class="mini" data-sh="copy" data-token="${esc(it.token)}">${ic('copy')}复制</button>
      <button class="mini danger" data-sh="revoke" data-token="${esc(it.token)}" title="撤销这个链接">${ic('trash')}</button>
    </div>`;
  }).join('');
  openModal('已分享的链接',
    `<p>共 <strong>${d.total}</strong> 个有效链接 · 拿到链接的人可免登录只读访问</p>
     <div class="tr-list">${rows || '<div class="tr-empty">还没有分享过任何文件</div>'}</div>`,
    [{ text: '关闭', cls: '', onClick: closeModal }]);
  $$('#modalBody [data-sh]').forEach(b => {
    b.onclick = async () => {
      const token = b.dataset.token;
      const it = items.find(x => x.token === token);
      if (b.dataset.sh === 'copy') {
        copyText(it ? it.url : '');
      } else if (b.dataset.sh === 'revoke') {
        try {
          await api('/share/revoke', 'POST', { token });
          toast('已撤销');
          closeModal();
        } catch (e) { toast('撤销失败：' + e.message, 'error'); }
      }
    };
  });
}

/* ---------------- 分享链接 ---------------- */
const SHAREABLE = ['image', 'video', 'audio', 'pdf', 'doc', 'sheet', 'slide',
                   'zip', 'text', 'code', 'web', 'data', 'script', 'other'];
async function copyText(t) {
  try {
    await navigator.clipboard.writeText(t);
    toast('已复制到剪贴板');
  } catch (e) {
    toast('复制失败，请手动选中', 'warn');
  }
}

function openShare(entry) {
  openModal('分享链接',
    `<p>为 <code>${esc(entry.name)}</code> 生成免登录的只读链接——拿到链接的人不用登录就能下载。</p>
     <label class="fl"><span>有效期</span>
       <select id="shareDays" class="select" style="width:100%;height:38px">
         <option value="1">1 天</option>
         <option value="7" selected>7 天</option>
         <option value="30">30 天</option>
       </select>
     </label>
     <div id="shareOut" class="share-out"></div>`,
    [
      { text: '关闭', cls: '', onClick: closeModal },
      { text: '生成链接', cls: 'primary', onClick: async () => {
          const days = parseInt($('#shareDays').value, 10) || 7;
          try {
            const d = await api('/share', 'POST', { path: entry.rel, days });
            $('#shareOut').innerHTML =
              `<p>${new Date(d.expires * 1000).toLocaleString()} 失效</p>
               <p class="share-url" id="shareUrl" title="点击复制">${esc(d.url)}</p>`;
            $('#shareUrl').onclick = () => copyText(d.url);
          } catch (e) { toast('生成失败：' + e.message, 'error'); }
        } },
    ]);
}

/* ---------------- 回收站 ---------------- */
async function trashAction(kind, id, name) {
  // 彻底删除不可恢复，必须二次确认；取消后要回到回收站（不是主目录）
  if (kind === 'purge') {
    return confirmBox('彻底删除',
      `<p>将<strong style="color:var(--err)">永久删除</strong>「${esc(name || '该项')}」，无法恢复。</p>` +
      '<p style="color:var(--faint);font-size:12px">如需保留，请改用「还原」。</p>',
      '彻底删除',
      () => doTrashAction('purge', id),
      () => openTrash());          // 取消 → 重新打开回收站
  }
  return doTrashAction(kind, id);
}

async function doTrashAction(kind, id) {
  try {
    const d = kind === 'restore'
      ? await api('/trash/restore', 'POST', { ids: [id] })
      : await api('/trash/purge', 'POST', { ids: [id] });
    toast(d.msg || '完成');
    load();
    openTrash();
  } catch (e) { toast('操作失败：' + e.message, 'error'); }
}

function confirmEmpty() {
  confirmBox('清空回收站',
    '<p>回收站里所有项目将被<strong style="color:var(--err)">永久删除</strong>，无法恢复。</p>',
    '永久清空',
    async () => {
      try {
        const d = await api('/trash/empty', 'POST', {});
        toast(d.msg || '已清空');
        load();
      } catch (e) { toast('清空失败：' + e.message, 'error'); }
    });
}

async function openTrash() {
  let d;
  try { d = await api('/api/trash'); }
  catch (e) { return toast('读取回收站失败：' + e.message, 'error'); }
  const items = d.items || [];
  const rows = items.map(m => `
    <div class="tr-row">
      <span class="tr-ico k-${m.is_dir ? 'dir' : 'other'}">${m.is_dir ? ic('folder', 'big') : ic('file', 'big')}</span>
      <span class="tr-main"><b>${esc(m.name)}</b><em>${esc(m.orig)}</em></span>
      <span class="tr-meta">${fmtTime(m.deleted_at)}${m.is_dir ? '' : '<br>' + fmtSize(m.size)}</span>
      <button class="mini" data-tr="restore" data-id="${esc(m.id)}">${ic('restore')}还原</button>
      <button class="mini danger" data-tr="purge" data-id="${esc(m.id)}" title="彻底删除">${ic('trash')}</button>
    </div>`).join('');
  openModal('回收站',
    `<p>共 <strong>${d.total}</strong> 项 · ${fmtSize(d.bytes)} · 保留 ${d.keep_days} 天后自动清除</p>
     <div class="tr-list">${rows || '<div class="tr-empty">回收站是空的</div>'}</div>`,
    [
      { text: '关闭', cls: '', onClick: closeModal },
    ].concat(items.length ? [{ text: '清空回收站', cls: 'danger', onClick: confirmEmpty }] : []));
  $$('#modalBody [data-tr]').forEach(b => {
    b.onclick = () => trashAction(b.dataset.tr, b.dataset.id,
      b.closest('.tr-row')?.querySelector('.tr-main b')?.textContent || '');
  });
}

/* ---------------- 视图 / 排序 ---------------- */
$('#viewToggle').addEventListener('click', e => {
  const b = e.target.closest('[data-view]');
  if (!b) return;
  S.view = b.dataset.view;
  localStorage.setItem('rs_view', S.view);
  $$('#viewToggle button').forEach(x => x.classList.toggle('on', x.dataset.view === S.view));
  load();
});
$('#sortSelect').value = S.sort;
$('#btnOrder').textContent = S.order === 'asc' ? '↑' : '↓';
$('#sortSelect').onchange = e => {
  S.sort = e.target.value; S.page = 1;
  localStorage.setItem('rs_sort', S.sort);
  load();
};
$('#btnOrder').onclick = () => {
  S.order = S.order === 'asc' ? 'desc' : 'asc';
  S.page = 1;
  localStorage.setItem('rs_order', S.order);
  $('#btnOrder').textContent = S.order === 'asc' ? '↑' : '↓';
  load();
};
$$('#viewToggle button').forEach(x => x.classList.toggle('on', x.dataset.view === S.view));
$('#btnRefresh').onclick = () => { S.sel.clear(); load(); };

/* ---------------- 搜索 ---------------- */
let searchTimer = null;
$('#btnContentSearch').onclick = () => {
  S.contentSearch = !S.contentSearch;
  localStorage.setItem('rs_content', S.contentSearch ? '1' : '0');
  $('#btnContentSearch').classList.toggle('on', S.contentSearch);
  if (S.q) {                       // 有搜索词就立刻按新范围重搜
    S.recursive = true;            // 内容搜索必须全库，否则只筛当前目录没意义
    S.page = 1;
    load();
  }
  toast(S.contentSearch ? '已开启内容搜索（全库）' : '已关闭内容搜索', S.contentSearch ? 'success' : 'warn');
};
if (S.contentSearch) $('#btnContentSearch').classList.add('on');

$('#searchInput').addEventListener('input', e => {
  const v = e.target.value.trim();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    S.q = v; S.recursive = false; S.page = 1; S.sel.clear();
    load();
  }, 320);
});
$('#searchInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    e.preventDefault();
    const v = e.target.value.trim();
    S.q = v; S.recursive = !!v; S.page = 1; S.sel.clear();
    if (v) toast(`全库搜索 “${v}”`);
    load();
  } else if (e.key === 'Escape') {
    e.target.value = ''; S.q = ''; S.recursive = false; S.page = 1; load(); e.target.blur();
  }
});

/* ---------------- 命令面板（Ctrl+K） ---------------- */
const COMMANDS = [
  { icon: 'file-plus', label: '新建文件', hint: '命令', run: () => $('#btnNewFile').click() },
  { icon: 'folder-plus', label: '新建文件夹', hint: '命令', run: () => $('#btnNewFolder').click() },
  { icon: 'upload', label: '上传文件', hint: '命令', run: () => $('#btnUpload').click() },
  { icon: 'trash', label: '打开回收站', hint: '命令', run: () => openTrash() },
  { icon: 'refresh', label: '刷新列表', hint: 'R', run: () => { S.sel.clear(); load(); } },
  { icon: 'folder', label: '回到根目录', hint: '命令', run: () => goRoot() },
  { icon: 'list', label: '视图：列表', hint: '命令', run: () => setView('list') },
  { icon: 'grid', label: '视图：网格', hint: '命令', run: () => setView('grid') },
  { icon: 'water', label: '视图：瀑布流', hint: '命令', run: () => setView('water') },
  { icon: 'search', label: '切换全文搜索', hint: '命令', run: () => $('#btnContentSearch').click() },
  { icon: 'share', label: '分享当前目录', hint: '命令', run: () => openShare({ rel: S.path, name: S.path || '文件库根目录' }) },
  { icon: 'share', label: '查看已分享的链接', hint: '命令', run: () => openShares() },
  { icon: 'sort', label: '排序：名称', hint: '命令', run: () => setSort('name') },
  { icon: 'clock', label: '排序：修改时间', hint: '命令', run: () => setSort('time') },
  { icon: 'sort', label: '排序：大小', hint: '命令', run: () => setSort('size') },
];

function goRoot() {
  S.path = ''; S.page = 1; S.q = ''; S.recursive = false; S.sel.clear();
  const si = $('#searchInput'); if (si) si.value = '';
  load();
}
function setView(v) {
  S.view = v;
  localStorage.setItem('rs_view', v);
  $$('#viewToggle button').forEach(x => x.classList.toggle('on', x.dataset.view === v));
  load();
}
function setSort(f) {
  S.sort = f; S.page = 1;
  localStorage.setItem('rs_sort', f);
  $('#sortSelect').value = f;
  load();
}

let palItems = [], palIdx = 0, palTimer = null, palFiles = [];

function openPalette() {
  const el = $('#palette');
  if (!el) return;
  el.classList.add('show');
  const inp = $('#palInput');
  inp.value = '';
  setTimeout(() => inp.focus(), 30);
  palFiles = []; palIdx = 0;
  renderPalette('');
}

function closePalette() {
  const el = $('#palette');
  if (el) el.classList.remove('show');
  palItems = []; palIdx = 0; palFiles = [];
}

function renderPalette(q) {
  const isCmd = q.startsWith('>');
  const term = (isCmd ? q.slice(1) : q).trim().toLowerCase();
  const cmds = COMMANDS.filter(c => !term || c.label.toLowerCase().includes(term));
  const files = isCmd ? [] : palFiles;
  palItems = [
    ...files.map(f => ({ type: 'file', ...f })),
    ...cmds.map(c => ({ type: 'cmd', ...c })),
  ];
  if (palIdx >= palItems.length) palIdx = 0;
  const body = $('#palBody');
  if (!palItems.length) {
    body.innerHTML = '<div class="pal-empty">没有匹配项</div>';
    return;
  }
  body.innerHTML = palItems.map((it, i) => {
    const isFile = it.type === 'file';
    return `<div class="pal-item${i === palIdx ? ' on' : ''}" data-i="${i}">
      <span class="${isFile ? 'k-' + it.kind : 'k-dir'}" style="display:grid;place-items:center">${isFile ? ico(it.kind) : ic(it.icon)}</span>
      <span class="pal-label">${esc(isFile ? it.name : it.label)}</span>
      <span class="pal-hint">${esc(isFile ? (it.rel || '') : (it.hint || ''))}</span>
    </div>`;
  }).join('');
  $$('#palBody .pal-item').forEach(el => {
    el.onmouseenter = () => { palIdx = +el.dataset.i; paintPalSel(); };
    el.onclick = () => runPalItem(palItems[+el.dataset.i]);
  });
}

function paintPalSel() {
  $$('#palBody .pal-item').forEach((el, i) => el.classList.toggle('on', i === palIdx));
  const cur = $('#palBody .pal-item.on');
  if (cur) cur.scrollIntoView({ block: 'nearest' });
}

function runPalItem(it) {
  if (!it) return;
  closePalette();
  if (it.type === 'cmd') { it.run(); return; }
  if (it.is_dir) {
    S.path = it.rel; S.page = 1; S.q = ''; S.recursive = false; S.sel.clear();
    $('#searchInput').value = '';
    load();
  } else {
    openLightbox(it);
  }
}

$('#palInput').addEventListener('input', e => {
  const q = e.target.value;
  renderPalette(q);
  clearTimeout(palTimer);
  const isCmd = q.startsWith('>');
  const term = (isCmd ? '' : q).trim();
  palFiles = [];
  if (term.length >= 2) {
    palTimer = setTimeout(async () => {
      try {
        const d = await api('/api/list?q=' + encodeURIComponent(term) + '&recursive=1'
                            + (S.contentSearch ? '&content=1' : ''));
        palFiles = d.entries.slice(0, 12);
        renderPalette($('#palInput').value);
      } catch (err) { /* 搜索失败就只显示命令 */ }
    }, 260);
  }
});

$('#palInput').addEventListener('keydown', e => {
  if (e.key === 'Escape') { e.stopPropagation(); return closePalette(); }
  if (e.key === 'ArrowDown') { e.preventDefault(); palIdx = Math.min(palIdx + 1, palItems.length - 1); paintPalSel(); }
  if (e.key === 'ArrowUp') { e.preventDefault(); palIdx = Math.max(palIdx - 1, 0); paintPalSel(); }
  if (e.key === 'Enter') { e.preventDefault(); runPalItem(palItems[palIdx]); }
});

$('#palette').addEventListener('click', e => { if (e.target.id === 'palette') closePalette(); });

document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
    e.preventDefault();
    if ($('#palette').classList.contains('show')) closePalette(); else openPalette();
  }
});

/* ---------------- 快捷键 ---------------- */
document.addEventListener('keydown', e => {
  const tag = (e.target.tagName || '').toLowerCase();
  const typing = tag === 'input' || tag === 'textarea' || tag === 'select';
  if (e.key === 'Escape') {
    if ($('#lightbox').classList.contains('show')) return closeLightbox();
    if ($('#modal').classList.contains('show')) return closeModal();
    if (typing) return;
    return;
  }
  if (typing) return;
  if (e.key === '/') { e.preventDefault(); $('#searchInput').focus(); return; }
  if (e.key === 'r' || e.key === 'R') { S.sel.clear(); load(); return; }
  if (e.key === 'a' && (e.ctrlKey || e.metaKey)) {
    e.preventDefault();
    S.entries.forEach(x => S.sel.add(x.rel));
    syncSel();
    return;
  }
  if (e.key === 'Delete' || e.key === 'Backspace') {
    if (S.sel.size) { e.preventDefault(); doDelete(Array.from(S.sel)); }
  }
});

/* ---------------- 启动 ---------------- */
load();
})();

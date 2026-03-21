const API = '/api/todos';
let allTodos = [];
let editingId = null;
let cmEditor = null;
let cmModules = null; // lazily loaded CodeMirror modules
let lastMtime = 0;
let pollTimer = null;
let selectedIdx = -1; // -1 = nothing, 0 = add-form, 1+ = todo items
let insertBeforeId = null; // when adding, insert before this todo id
let searchQuery = ''; // fuzzy search filter
let showPriorities = new Set();  // colored: only show these
let hidePriorities = new Set();  // greyed: hide these
let ctxTargetId = null; // id of todo targeted by context menu
let visibleIds = []; // ordered list of todo ids as rendered
let sectionsOrder = []; // ordered list of section names as rendered
let addFormVisible = false;
let filterActiveSessions = false; // only show items with active terminal sessions
let filterUnread = false; // only show items with unread updates
let previewMode = false; // auto-expand selected item
let previewExpandedId = null; // item currently auto-expanded by preview
const expandedItems = new Set(); // items whose descriptions are expanded
const collapsedSections = new Set(['__completed__']); // collapsed section names
const _seenUpdates = new Set(); // todo IDs whose ⚡ update has been viewed
const SEL_ADD = 0; // index for the add-form position

async function loadTodos() {
  const res = await fetch(API);
  allTodos = await res.json();
  // Update our known mtime so polling doesn't re-trigger
  try {
    const mt = await fetch(API + '/mtime');
    const d = await mt.json();
    lastMtime = d.mtime;
  } catch(e) {}
  render();
}

// Poll for external file changes every 1.5s
async function pollForChanges() {
  try {
    const res = await fetch(API + '/mtime');
    const data = await res.json();
    if (data.mtime !== lastMtime) {
      lastMtime = data.mtime;
      // Don't reload if user is editing
      if (!editingId) {
        const res2 = await fetch(API);
        allTodos = await res2.json();
        render();
      }
    }
  } catch(e) {}
  // Also poll jobs and terminal sessions
  await pollJobs();
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(pollForChanges, 500);
}

function render() {
  const active = allTodos.filter(t => t.status !== 'completed');
  const completed = allTodos.filter(t => t.status === 'completed');

  const activeEl = document.getElementById('active-section');
  const completedEl = document.getElementById('completed-section');

  // Rescue add-form before innerHTML overwrites it (it may be inside activeEl)
  const form = document.getElementById('add-form');
  const formWasVisible = addFormVisible;
  const formTitle = form.querySelector('#new-title')?.value || '';
  const formDesc = form.querySelector('#new-desc')?.value || '';
  const formPriority = form.querySelector('#new-priority')?.value || 'medium';
  const formSection = form.querySelector('#new-section')?.value || '';
  const formSectionCustom = form.querySelector('#new-section-custom')?.value || '';
  const searchBar = document.querySelector('.search-bar');
  if (searchBar) searchBar.after(form);

  // Apply search and priority filters
  const searching = searchQuery.trim().length > 0;

  const searchTokens = searching ? searchQuery.toLowerCase().trim().split(/\s+/) : [];
  const matchesSearch = t => {
    const text = ((t.title || '') + ' ' + (t.description || '') + ' ' + (t.section || '')).toLowerCase();
    return searchTokens.every(tok => text.includes(tok));
  };
  const afterSearch = f => searching ? f.filter(matchesSearch) : f;
  const afterPriority = f => {
    if (showPriorities.size === 0 && hidePriorities.size === 0) return f;
    return f.filter(t => {
      const p = t.priority || 'medium';
      if (showPriorities.size > 0) return showPriorities.has(p);
      return !hidePriorities.has(p);
    });
  };
  const afterSessions = f => {
    if (!filterActiveSessions) return f;
    // Sort by activity: active sessions/chats/unread first (most recent on top), rest below
    const activityScore = t => {
      const hasTerminal = _termSessions[t.id] && _termSessions[t.id].alive;
      const hasChatStream = _chatSessions[t.id] && _chatSessions[t.id].streamingJobId;
      const hasUnreadChat = _chatUnread.has(t.id);
      const hasUpdated = _chatUnread.has(t.id);
      const isActive = hasTerminal || hasChatStream || hasUnreadChat || hasUpdated;
      if (!isActive) return 0;
      // Find most recent job for this todo
      let latest = 0;
      for (const j of Object.values(_jobsState)) {
        if (_todoIdForJob(j) === t.id && j.created_at > latest) latest = j.created_at;
      }
      return latest || 1; // 1 = active but no job timestamp
    };
    return [...f].sort((a, b) => activityScore(b) - activityScore(a));
  };
  const afterUnread = f => {
    if (!filterUnread) return f;
    return f.filter(t => _chatUnread.has(t.id));
  };
  const filteredActive = afterUnread(afterSessions(afterPriority(afterSearch(active))));
  const filteredCompleted = afterUnread(afterSessions(afterPriority(afterSearch(completed))));


  if (filteredActive.length === 0 && filteredCompleted.length === 0 && !searching && allTodos.length === 0) {
    activeEl.innerHTML = '<div class="empty-state">No todos yet. Press <strong>n</strong> to add one!</div>';
    completedEl.innerHTML = '';
    visibleIds = [];
    if (formWasVisible) _restoreInlineForm(form, formTitle, formDesc, formPriority, formSection, formSectionCustom);
    applySelection();
    return;
  }

  // Group active by section preserving order of first appearance
  let activeHtml = '';
  const visibleActive = []; // track which active items are visible (not collapsed)
  const visibleActiveIds = []; // mix of todo IDs and '__section__:Name' markers for collapsed sections

  if (filterActiveSessions) {
    // Flat list, no section grouping — sorted by activity
    sectionsOrder = [];
    activeHtml = filteredActive.map(t => renderTodo(t)).join('');
    visibleActive.push(...filteredActive);
    visibleActiveIds.push(...filteredActive.map(t => t.id));
  } else {
    sectionsOrder = [];
    const seenSections = new Set();
    filteredActive.forEach(t => {
      const s = t.section || '';
      if (!seenSections.has(s)) { sectionsOrder.push(s); seenSections.add(s); }
    });

    sectionsOrder.forEach(section => {
      const items = filteredActive.filter(t => (t.section || '') === section);
      const isCollapsed = !searching && collapsedSections.has(section);
      const escSection = esc(section).replace(/'/g, "\\'");
      if (section) {
        activeHtml += `<div class="section-header-row" data-section="${esc(section)}" draggable="true">`
          + `<button class="collapse-btn${isCollapsed ? ' collapsed' : ''}" onclick="toggleSectionCollapse('${escSection}')" title="${isCollapsed ? 'Expand' : 'Collapse'}">&#9660;</button>`
          + `<h3 onclick="toggleSectionCollapse('${escSection}')" ondblclick="startSectionRename('${escSection}')">${esc(section)}</h3>`
          + `<span class="section-count">${items.length}</span>`
          + `<button class="sort-priority-btn" onclick="sortByPriority('${escSection}')" title="Sort by priority (high first)">&#9650; Priority</button>`
          + `</div>`;
      }
      if (isCollapsed) {
        if (section) visibleActiveIds.push('__section__:' + section);
      } else {
        activeHtml += items.map(t => renderTodo(t)).join('');
        visibleActive.push(...items);
        visibleActiveIds.push(...items.map(t => t.id));
      }
    });
  }

  // visibleIds includes todo IDs + section markers for collapsed sections
  visibleIds = [...visibleActiveIds, ...filteredCompleted.map(t => t.id)];

  const eaBtn = '<div class="ea-update-wrap"><button id="ea-update-btn" class="btn btn-sm ea-update-btn" onclick="eaUpdateToggle()" title="Run /ea update"><span class="ea-btn-wrap"><span id="ea-update-label" class="ea-lbl" style="opacity:1">Update</span><span id="ea-update-running" class="ea-running" style="opacity:0"><l-mirage size="28" speed="2.5" color="' + getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() + '"></l-mirage><span id="ea-update-timer">0:00</span></span></span></button><div id="ea-update-bubble" class="ea-update-bubble"></div></div>';
  const totalItems = filteredActive.length;
  const expandedCount = filteredActive.filter(t => expandedItems.has(t.id)).length;
  const simpleCls = expandedCount === 0 ? ' active' : (expandedCount < totalItems ? ' partial' : '');
  const simpleBtn = `<button class="header-toggle simple-toggle-btn${simpleCls}" onclick="toggleSimpleMode()" title="Toggle simple mode (a)">Simple</button>`;
  const pColors = {high:'#b91c1c',medium:'#a16207',low:'#15803d',none:'#9ca3af'};
  const pBg = {high:'#fef2f2',medium:'#fffbeb',low:'#f0fdf4',none:'#f3f4f6'};
  const pBorder = {high:'#fecaca',medium:'#fde68a',low:'#bbf7d0',none:'#e5e7eb'};
  const filterBtns = ['high','medium','low','none'].map(p => {
    const isShow = showPriorities.has(p);
    const isHide = hidePriorities.has(p);
    let style;
    if (isShow) style = `background:${pColors[p]};color:#fff;border-color:${pColors[p]}`;
    else if (isHide) style = `background:var(--border);color:var(--subtle);border-color:var(--border);text-decoration:line-through`;
    else style = `background:${pBg[p]};color:${pColors[p]};border-color:${pBorder[p]}`;
    const label = {high:'H',medium:'M',low:'L',none:'0'}[p];
    return `<button class="header-toggle" style="${style}" onclick="cycleFilter('${p}')" title="Filter ${p}">${label}</button>`;
  }).join('');
  const previewBtn = `<button class="header-toggle preview-toggle-btn${previewMode ? ' active' : ''}" onclick="togglePreviewMode()" title="Preview mode: auto-expand selected (v)">Preview</button>`;
  const sessionsBtn = `<button class="header-toggle${filterActiveSessions ? ' active' : ''}" onclick="toggleFilterSessions()" title="Filter by active sessions (Alt+S)" style="${filterActiveSessions ? '' : 'color:var(--subtle)'}">Sessions</button>`;
  const unreadBtn = `<button class="header-toggle${filterUnread ? ' active' : ''}" onclick="toggleFilterUnread()" title="Filter by unread updates" style="${filterUnread ? 'background:#f59e0b;color:#fff;border-color:#f59e0b' : 'color:#f59e0b;border-color:#f59e0b'}">Unread</button>`;
  const activeSections = sectionsOrder.filter(s => s);
  const collapsedCount = activeSections.filter(s => collapsedSections.has(s)).length;
  const allCollapsed = activeSections.length > 0 && collapsedCount === activeSections.length;
  const collapseAllBtn = activeSections.length === 0 ? '' : `<button class="collapse-btn${allCollapsed ? ' collapsed' : ''}" onclick="toggleCollapseAll()" title="Collapse/expand all sections">&#9660;</button>`;
  const modeGroup = `<span class="btn-group">${simpleBtn}${previewBtn}${sessionsBtn}${unreadBtn}</span>`;
  const headerBtns = '<div class="active-header-btns">' + collapseAllBtn + '<h2>Active' + (filteredActive.length ? ' (' + filteredActive.length + ')' : '') + '</h2>' + filterBtns + modeGroup + '</div>' + eaBtn;
  activeEl.innerHTML = filteredActive.length
    ? '<div class="active-header">' + headerBtns + '</div>' + activeHtml
    : '<div class="active-header">' + headerBtns + '</div><div class="empty-state">All done! &#127881;</div>';

  const isCompletedCollapsed = !searching && collapsedSections.has('__completed__');
  if (filteredCompleted.length) {
    completedEl.innerHTML = `<div class="section-header-row" data-section="__completed__">`
      + `<button class="collapse-btn${isCompletedCollapsed ? ' collapsed' : ''}" onclick="toggleSectionCollapse('__completed__')" title="${isCompletedCollapsed ? 'Expand' : 'Collapse'}">&#9660;</button>`
      + `<h2 style="margin:0;">Completed (${filteredCompleted.length})</h2>`
      + `</div>`
      + (isCompletedCollapsed ? '' : filteredCompleted.map(t => renderTodo(t)).join(''));
    if (isCompletedCollapsed) {
      visibleIds = [...visibleActiveIds, '__section__:__completed__'];
    }
  } else {
    completedEl.innerHTML = '';
  }

  // Update section dropdown options
  const allSections = [...new Set(allTodos.map(t => t.section || '').filter(Boolean))];
  const secSelect = document.getElementById('new-section');
  if (secSelect) {
    const curVal = secSelect.value;
    secSelect.innerHTML = '<option value="">No section</option>'
      + allSections.map(s => `<option value="${esc(s)}">${esc(s)}</option>`).join('')
      + '<option value="__custom__">Other...</option>';
    // Restore previous selection if still valid
    if ([...secSelect.options].some(o => o.value === curVal)) secSelect.value = curVal;
  }

  // Restore inline form if it was visible and we have an insertion target
  if (formWasVisible) _restoreInlineForm(form, formTitle, formDesc, formPriority, formSection, formSectionCustom);

  // Clamp selectedIdx if items disappeared (e.g. section collapsed)
  if (selectedIdx > visibleIds.length) selectedIdx = visibleIds.length > 0 ? visibleIds.length : -1;

  // Set sticky offsets for stacking: search bar → active header → section headers + hero spinner
  const stickyEl = document.querySelector('.sticky-header');
  const activeHeaderEl = document.querySelector('.active-header');
  const searchH = stickyEl ? stickyEl.offsetHeight : 0;
  document.documentElement.style.setProperty('--sticky-offset', searchH + 'px');
  const activeH = activeHeaderEl ? activeHeaderEl.offsetHeight : 0;
  document.documentElement.style.setProperty('--section-offset', (searchH + activeH) + 'px');

  applySelection();
  _restoreJobOutputs();
  _restoreEaUpdateBubble();
}


function _restoreInlineForm(form, title, desc, priority, section, sectionCustom) {
  if (insertBeforeId) {
    const targetEl = document.querySelector(`.todo-item[data-todo-id="${insertBeforeId}"]`);
    if (targetEl) targetEl.parentNode.insertBefore(form, targetEl);
  }
  form.classList.add('visible');
  form.querySelector('#new-title').value = title;
  form.querySelector('#new-desc').value = desc;
  form.querySelector('#new-priority').value = priority;
  // Restore section select — if the value exists in options, set it; otherwise set to custom
  const secSelect = form.querySelector('#new-section');
  const customInput = form.querySelector('#new-section-custom');
  if ([...secSelect.options].some(o => o.value === section)) {
    secSelect.value = section;
  } else if (section) {
    secSelect.value = '__custom__';
  }
  customInput.value = sectionCustom;
  customInput.style.display = secSelect.value === '__custom__' ? '' : 'none';
}

function renderTodo(t) {
  const checked = t.status === 'completed' ? 'checked' : '';
  const priorityClass = t.priority === 'none' ? 'priority-none-item' : (t.priority === 'high' ? 'priority-high-item' : '');
  const statusClass = t.status === 'completed' ? 'status-completed' : priorityClass;

  if (editingId === t.id) {
    return `<div class="todo-item ${statusClass}">
      <div class="todo-body">
        <input class="edit-title" id="edit-title-${t.id}" value="${esc(_parseTitle(t.title || '').displayTitle)}">
        <div class="edit-desc-cm" id="edit-desc-${t.id}"></div>
        <select id="edit-section-${t.id}" class="edit-select">
          <option value="">No section</option>
          ${allSectionsForEdit().map(s => `<option value="${esc(s)}" ${(t.section||'')===s?'selected':''}>${esc(s)}</option>`).join('')}
          <option value="__custom__">Other...</option>
        </select>
        <input class="edit-title" id="edit-section-custom-${t.id}" placeholder="New section name" style="display:none;">
        <div class="edit-actions">
          <select id="edit-priority-${t.id}" class="edit-select" style="width:auto;margin-bottom:0">
            ${['high','medium','low','none'].map(p =>
              `<option value="${p}" ${p===t.priority?'selected':''}>${p}</option>`
            ).join('')}
          </select>
          <button class="btn btn-primary btn-sm" onclick="saveEdit('${t.id}')">Save <span style="opacity:0.6;font-weight:400">&#8984;&#9166;</span></button>
          <button class="btn btn-sm" onclick="cancelEdit()" style="border:1px solid var(--border)">Cancel <span style="opacity:0.6;font-weight:400">Esc</span></button>
        </div>
      </div>
    </div>`;
  }

  const descHtml = t.description ? renderMd(t.description).replace(/conv:([a-zA-Z0-9_-]+)/g, `<a href="#" class="conv-link" onclick="event.preventDefault();event.stopPropagation();resumeConv('$1','${t.id}')" title="Resume conversation $1">conv:$1</a>`) : '';
  const desc = descHtml ? `<div class="todo-desc">${descHtml}</div>` : '';
  const priorityBadge = `<span class="priority-badge priority-${t.priority || 'medium'}">${t.priority || 'medium'}</span>`;

  const activeJob = _getActiveJobForTodo(t.id);
  const isRunning = activeJob && activeJob.status === 'running' && !(activeJob.job_key && activeJob.job_key.startsWith('chat-'));
  const spinner = isRunning ? `<span class="job-spinner" title="Stop job" onclick="event.stopPropagation();killJob('${activeJob.id}')"><l-jelly-triangle size="13" speed="1.75" color="var(--accent)"></l-jelly-triangle></span>` : '';
  const jobBubble = `<div class="checkon-bubble" id="checkon-bubble-${t.id}"></div>`;
  const jobSummary = `<div class="checkon-summary" id="checkon-summary-${t.id}"></div>`;

  const draggable = t.status !== 'completed' ? 'draggable="true"' : '';
  const itemToggled = expandedItems.has(t.id) ? ' item-expanded' : '';
  const isCompleted = t.status === 'completed';
  const swipeRevealClass = isCompleted ? 'swipe-reveal swipe-reveal-undo' : 'swipe-reveal';
  const swipeIcon = isCompleted ? '&#8634;' : '&#10003;';
  return `<div class="todo-item ${statusClass}${itemToggled}" data-todo-id="${t.id}" onclick="selectTodo('${t.id}')" ondblclick="startEdit('${t.id}')" oncontextmenu="showCtxMenu(event,'${t.id}')" style="cursor:pointer;">
    <div class="${swipeRevealClass}"><span class="swipe-reveal-icon">${swipeIcon}</span></div>
    <div class="swipe-reveal swipe-reveal-delete"><span class="swipe-reveal-icon">&#128465;</span></div>
    <div class="swipe-content">
    <div class="todo-header">
      <div class="todo-title" ${draggable} style="flex:1;min-width:0;display:flex;align-items:center;gap:2px;${t.status !== 'completed' ? 'cursor:grab;' : ''}" onclick="event.stopPropagation();selectTodo('${t.id}');toggleItemDesc('${t.id}')">${spinner}${esc(_parseTitle(t.title || '').displayTitle)}</div>
      <div class="todo-actions">
        ${t.status !== 'completed' ? `<button onclick="event.stopPropagation();eaUpdateItem('${t.id}')" style="border:none;background:transparent;font-size:1rem;padding:4px 6px;cursor:pointer;color:var(--subtle);line-height:1;transition:color .15s" title="Refresh via /ea checkon" onmouseover="this.style.color='var(--accent)'" onmouseout="this.style.color='var(--subtle)'">&#8635;</button>` : ''}
        <button onclick="event.stopPropagation();consolidateItem('${t.id}')" style="border:none;background:transparent;font-size:0.85rem;padding:4px 6px;cursor:pointer;color:var(--subtle);line-height:1;transition:color .15s" title="Consolidate description" onmouseover="this.style.color='var(--accent)'" onmouseout="this.style.color='var(--subtle)'">&#x29C9;</button>
        ${t.status !== 'completed' ? `<button onclick="event.stopPropagation();openChat('${t.id}')" style="border:none;background:transparent;font-size:1rem;padding:4px 6px;cursor:pointer;color:var(--subtle);line-height:1;transition:color .15s" title="Chat (s)" onmouseover="this.style.color='var(--accent)'" onmouseout="this.style.color='var(--subtle)'">&#9654;</button>` : ''}
      </div>
      ${priorityBadge}
    </div>
    ${desc}${jobSummary}${jobBubble}
    </div>
  </div>`;
}

function _parseTitle(raw) {
  // Extract bold text as display title: **title text**
  const boldMatch = raw.match(/\*\*(.+?)\*\*/);
  const stripped = raw.replace(/`(?:updated|read)[^`]*`/g, '').replace(/\*\*/g, '').trim();
  const displayTitle = boldMatch ? boldMatch[1] : stripped;
  const hasUpdatedTag = /`updated\s[^`]*`/.test(raw);
  return { displayTitle, hasUpdatedTag };
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function renderMd(s) {
  if (!s) return '';
  try {
    const renderer = new marked.Renderer();
    renderer.link = function(token) {
      const t = token.title ? ` title="${token.title}"` : '';
      return `<a href="${token.href}"${t} target="_blank" rel="noopener noreferrer">${token.text}</a>`;
    };
    return marked.parse(s, {breaks: true, renderer});
  } catch(e) {
    return esc(s);
  }
}

function selectTodo(id) {
  const idx = visibleIds.indexOf(id);
  if (idx >= 0) {
    selectedIdx = idx + 1;
    applySelection();
  }
}

// --- Context menu ---
function showCtxMenu(e, id) {
  e.preventDefault();
  e.stopPropagation();
  ctxTargetId = id;
  selectTodo(id);

  const todo = allTodos.find(t => t.id === id);
  if (!todo) return;

  const allSections = [...new Set(allTodos.map(t => t.section || '').filter(Boolean))];
  const curSection = todo.section || '';

  let sectionItems = allSections.map(s => {
    const isCur = s === curSection;
    return `<div class="ctx-menu-item${isCur ? ' active-section' : ''}" onclick="ctxMoveSection('${esc(s).replace(/'/g, "\\'")}')">` +
           `${esc(s)}${isCur ? ' &#10003;' : ''}</div>`;
  }).join('');
  sectionItems += `<div class="ctx-menu-sep"></div>`;
  sectionItems += `<div class="ctx-menu-item" onclick="ctxMoveSectionNew()">New section&hellip;</div>`;
  if (curSection) {
    sectionItems += `<div class="ctx-menu-item" onclick="ctxMoveSection('')">Remove from section</div>`;
  }

  const menu = document.getElementById('ctx-menu');

  const curPriority = todo.priority || 'medium';
  let priorityItems = ['high','medium','low','none'].map(p => {
    const isCur = p === curPriority;
    return `<div class="ctx-menu-item${isCur ? ' active-section' : ''}" onclick="ctxSetPriority('${p}')">${p}${isCur ? ' &#10003;' : ''}</div>`;
  }).join('');

  menu.innerHTML =
    `<div class="ctx-menu-item" onclick="ctxEdit()">&#9998; Edit</div>` +
    `<div class="ctx-menu-item has-submenu">&#128193; Move to section<div class="ctx-submenu">${sectionItems}</div></div>` +
    `<div class="ctx-menu-item has-submenu">&#9873; Set priority<div class="ctx-submenu">${priorityItems}</div></div>` +
    `<div class="ctx-menu-item" onclick="hideCtxMenu();toggleTodoHistory(ctxTargetId)">&#128336; History</div>` +
    `<div class="ctx-menu-sep"></div>` +
    `<div class="ctx-menu-item" style="color:var(--danger)" onclick="ctxDelete()">&#128465; Delete</div>`;

  // Position: keep within viewport
  menu.classList.add('visible');
  const mw = menu.offsetWidth, mh = menu.offsetHeight;
  let x = e.clientX, y = e.clientY;
  if (x + mw > window.innerWidth) x = window.innerWidth - mw - 8;
  if (y + mh > window.innerHeight) y = window.innerHeight - mh - 8;
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
}

function hideCtxMenu() {
  document.getElementById('ctx-menu').classList.remove('visible');
  ctxTargetId = null;
}

function ctxEdit() {
  const id = ctxTargetId;
  hideCtxMenu();
  if (id) startEdit(id);
}

function ctxDelete() {
  const id = ctxTargetId;
  hideCtxMenu();
  if (id) deleteTodo(id);
}

async function ctxMoveSection(section) {
  const id = ctxTargetId;
  const prevIdx = selectedIdx;
  hideCtxMenu();
  if (!id) return;
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({section})
  });
  await loadTodos();
  selectedIdx = Math.min(prevIdx, visibleIds.length);
  if (selectedIdx < 1 && visibleIds.length > 0) selectedIdx = 1;
  applySelection();
}

function ctxMoveSectionNew() {
  hideCtxMenu();
  const name = prompt('New section name:');
  if (name !== null && name.trim()) {
    ctxTargetId && ctxMoveSection(name.trim());
  }
}

async function ctxSetPriority(priority) {
  const id = ctxTargetId;
  hideCtxMenu();
  if (!id) return;
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({priority})
  });
  loadTodos();
}

async function sortByPriority(section) {
  await fetch(API + '/sort-priority', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({section})
  });
  loadTodos();
}

// Close context menu on click outside or Escape
document.addEventListener('click', () => { hideCtxMenu(); if (sectionPickerOpen) hideSectionPicker(); });
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.getElementById('ctx-menu').classList.contains('visible')) {
    hideCtxMenu();
  }
});

function updateSimpleBtn() {
  const btn = document.querySelector('.simple-toggle-btn');
  if (!btn) return;
  const totalItems = allTodos.filter(t => t.status !== 'completed').length;
  const expandedCount = expandedItems.size;
  btn.classList.toggle('active', expandedCount === 0);
  btn.classList.toggle('partial', expandedCount > 0 && expandedCount < totalItems);
}

function toggleFilterSessions() {
  filterActiveSessions = !filterActiveSessions;
  render();
}

function toggleFilterUnread() {
  filterUnread = !filterUnread;
  render();
}

function toggleSimpleMode() {
  if (expandedItems.size > 0) {
    expandedItems.clear();
  } else {
    allTodos.filter(t => t.status !== 'completed').forEach(t => expandedItems.add(t.id));
  }
  render();
}

function togglePreviewMode() {
  previewMode = !previewMode;
  if (!previewMode && previewExpandedId) {
    const el = document.querySelector(`.todo-item[data-todo-id="${previewExpandedId}"]`);
    if (el) el.classList.remove('preview-expanded');
    previewExpandedId = null;
  }
  updatePreviewBtn();
  if (previewMode) applySelection();
}

function updatePreviewBtn() {
  const btn = document.querySelector('.preview-toggle-btn');
  if (btn) btn.classList.toggle('active', previewMode);
}

function cycleFilter(p) {
  if (showPriorities.has(p)) {
    showPriorities.delete(p);
    hidePriorities.add(p);
  } else if (hidePriorities.has(p)) {
    hidePriorities.delete(p);
  } else {
    showPriorities.add(p);
  }
  selectedIdx = -1;
  render();
}

function toggleItemDesc(id) {
  const el = document.querySelector(`.todo-item[data-todo-id="${id}"]`);
  if (!el) return;
  if (previewExpandedId === id) {
    el.classList.remove('preview-expanded');
    previewExpandedId = null;
  }
  if (expandedItems.has(id)) {
    expandedItems.delete(id);
    el.classList.remove('item-expanded');
    // Clear unread when collapsing (user has seen it)
    if (_viewedItems.has(id)) {
      _viewedItems.delete(id);
      _clearUnread(id);
    }
  } else {
    expandedItems.add(id);
    el.classList.add('item-expanded');
    // Track that user viewed this item
    _viewedItems.add(id);
  }
  updateSimpleBtn();
}

function toggleSectionCollapse(section) {
  if (collapsedSections.has(section)) collapsedSections.delete(section);
  else collapsedSections.add(section);
  render();
}

function collapseStep() {
  // First collapse all items, then collapse all sections
  if (expandedItems.size > 0) {
    expandedItems.clear();
    render();
    return;
  }
  const secs = sectionsOrder.filter(s => s);
  if (secs.length > 0 && !secs.every(s => collapsedSections.has(s))) {
    secs.forEach(s => collapsedSections.add(s));
    render();
  }
}

function expandStep() {
  // First expand all sections, then expand all items
  const secs = sectionsOrder.filter(s => s);
  if (secs.length > 0 && secs.some(s => collapsedSections.has(s))) {
    secs.forEach(s => collapsedSections.delete(s));
    render();
    return;
  }
  const activeTodos = allTodos.filter(t => t.status !== 'completed');
  if (activeTodos.some(t => !expandedItems.has(t.id))) {
    activeTodos.forEach(t => expandedItems.add(t.id));
    render();
  }
}

function toggleCollapseAll() {
  const secs = sectionsOrder.filter(s => s);
  const allCollapsed = secs.length > 0 && secs.every(s => collapsedSections.has(s));
  if (allCollapsed) {
    secs.forEach(s => collapsedSections.delete(s));
  } else {
    secs.forEach(s => collapsedSections.add(s));
  }
  render();
}

function getSectionOfSelected() {
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return null;
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo) return null;
  return todo.status === 'completed' ? '__completed__' : (todo.section || '');
}

function getNextCollapsedSection() {
  // Find the nearest collapsed section relative to current selection
  // Strategy: look at all sections in order and find the first collapsed one
  // at or after the selected item's position, or the last one before it
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) {
    // Nothing selected — just expand the first collapsed section
    for (const s of sectionsOrder) {
      if (collapsedSections.has(s)) return s;
    }
    if (collapsedSections.has('__completed__')) return '__completed__';
    return null;
  }
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo) return collapsedSections.values().next().value || null;
  const curSection = todo.status === 'completed' ? '__completed__' : (todo.section || '');

  // Look for a collapsed section immediately following the current section
  const allSecs = [...sectionsOrder, '__completed__'];
  const curIdx = allSecs.indexOf(curSection);
  // Search forward first, then backward
  for (let i = curIdx + 1; i < allSecs.length; i++) {
    if (collapsedSections.has(allSecs[i])) return allSecs[i];
  }
  for (let i = curIdx - 1; i >= 0; i--) {
    if (collapsedSections.has(allSecs[i])) return allSecs[i];
  }
  return null;
}

function allSectionsForEdit() {
  return [...new Set(allTodos.map(t => t.section || '').filter(Boolean))];
}

function getSelectedSection() {
  const sel = document.getElementById('new-section');
  if (sel.value === '__custom__') return document.getElementById('new-section-custom').value.trim();
  return sel.value;
}

function onSectionChange() {
  const sel = document.getElementById('new-section');
  const customInput = document.getElementById('new-section-custom');
  if (sel.value === '__custom__') {
    customInput.style.display = '';
    customInput.focus();
  } else {
    customInput.style.display = 'none';
    customInput.value = '';
  }
}
document.getElementById('new-section').addEventListener('change', onSectionChange);

async function addTodo() {
  const title = document.getElementById('new-title').value.trim();
  if (!title) return;
  const desc = document.getElementById('new-desc').value.trim();
  const priority = document.getElementById('new-priority').value;
  const section = getSelectedSection();
  const payload = {title, description: desc, priority, section};
  if (insertBeforeId) payload.before_id = insertBeforeId;
  const res = await fetch(API, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  const newTodo = await res.json();
  insertBeforeId = null;
  document.getElementById('new-title').value = '';
  document.getElementById('new-desc').value = '';
  document.getElementById('new-priority').value = 'medium';
  document.getElementById('new-section').value = '';
  document.getElementById('new-section-custom').value = '';
  document.getElementById('new-section-custom').style.display = 'none';
  hideAddForm();
  searchQuery = '';
  const searchEl = document.getElementById('search-input');
  searchEl.value = '';
  searchEl.classList.remove('has-query');
  await loadTodos();
  // Advance cursor to the newly created item
  if (newTodo && newTodo.id) {
    const idx = visibleIds.indexOf(newTodo.id);
    if (idx >= 0) {
      selectedIdx = idx + 1;
      applySelection();
    }
  }
}

async function toggleComplete(id, checked) {
  const todo = allTodos.find(t => t.id === id);
  const newStatus = checked ? 'completed' : 'open';
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status: newStatus})
  });
  loadTodos();
}

async function changePriority(id, priority) {
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({priority})
  });
  loadTodos();
}

async function deleteTodo(id) {
  if (!confirm('Delete this todo?')) return;
  await fetch(API + '/' + id, {method: 'DELETE'});
  loadTodos();
}

function navigateSection(direction) {
  // Snap to first/last in current section, or jump to adjacent section if already at edge
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return;
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo) return;
  const curSection = todo.status === 'completed' ? '__completed__' : (todo.section || '');

  // Build index ranges for each section in visibleIds
  const sectionRanges = []; // [{section, start, end}] (1-based indices into visibleIds)
  let prev = null;
  for (let i = 0; i < visibleIds.length; i++) {
    const t = allTodos.find(x => x.id === visibleIds[i]);
    const sec = t && t.status === 'completed' ? '__completed__' : (t ? (t.section || '') : '');
    if (sec !== prev) {
      sectionRanges.push({section: sec, start: i + 1, end: i + 1});
      prev = sec;
    } else {
      sectionRanges[sectionRanges.length - 1].end = i + 1;
    }
  }

  const rangeIdx = sectionRanges.findIndex(r => r.section === curSection && selectedIdx >= r.start && selectedIdx <= r.end);
  if (rangeIdx < 0) return;
  const range = sectionRanges[rangeIdx];

  if (direction === 'down') {
    if (rangeIdx + 1 < sectionRanges.length) {
      selectedIdx = sectionRanges[rangeIdx + 1].start;
    }
  } else {
    if (rangeIdx - 1 >= 0) {
      selectedIdx = sectionRanges[rangeIdx - 1].start;
    }
  }
  applySelection();
}

async function moveToAdjacentSection(direction) {
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return;
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo || todo.status === 'completed') return;
  const curSection = todo.section || '';
  const curIdx = sectionsOrder.indexOf(curSection);
  let newIdx = direction === 'down' ? curIdx + 1 : curIdx - 1;
  if (newIdx < 0 || newIdx >= sectionsOrder.length) return;
  const newSection = sectionsOrder[newIdx];
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({section: newSection})
  });
  const prevIdx = selectedIdx;
  await loadTodos();
  // Stay at the original position (select the next item that took its place)
  selectedIdx = Math.min(prevIdx, visibleIds.length);
  if (selectedIdx < 1 && visibleIds.length > 0) selectedIdx = 1;
  applySelection();
}

async function performUndo() {
  const res = await fetch('/api/undo', { method: 'POST', headers: {'Content-Type': 'application/json'} });
  if (res.ok) {
    await loadTodos();
    const toast = document.createElement('div');
    toast.innerHTML = 'Undone <span style="margin-left:8px;opacity:0.5;font-size:0.75rem">\u2318Z</span>';
    toast.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--text);color:var(--card);padding:8px 16px;border-radius:8px;font-size:0.85rem;z-index:2000;box-shadow:var(--shadow-lg);opacity:0;transition:opacity .15s';
    document.body.appendChild(toast);
    requestAnimationFrame(() => { toast.style.opacity = '1'; });
    setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 150); }, 1200);
  }
}

function copyTodoId(id) {
  function showToast() {
    const toast = document.createElement('div');
    toast.textContent = 'Copied ID: ' + id;
    toast.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--text);color:var(--card);padding:8px 16px;border-radius:8px;font-size:0.85rem;font-family:monospace;z-index:2000;box-shadow:var(--shadow-lg);opacity:0;transition:opacity .15s';
    document.body.appendChild(toast);
    requestAnimationFrame(() => { toast.style.opacity = '1'; });
    setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 150); }, 1500);
  }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(id).then(showToast).catch(() => { fallbackCopy(id); showToast(); });
  } else {
    fallbackCopy(id); showToast();
  }
}

async function startInTmux(id) {
  await openTerminal(id);
}

async function openTerminal(todoId, resumeId) {
  // If already have a live session with a terminal for this todo and not resuming, just switch to it
  if (!resumeId && _termSessions[todoId] && _termSessions[todoId].alive && _termSessions[todoId].term) {
    _showTerminalOverlay(todoId);
    return;
  }

  // Register session on server
  let data;
  try {
    const body = resumeId ? { resume_id: resumeId } : {};
    const res = await fetch(API + '/' + todoId + '/terminal', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!res.ok) { showToast('Failed to open terminal', true); return; }
    data = await res.json();
  } catch (e) {
    showToast('Failed to open terminal', true);
    return;
  }

  const sessionId = data.session_id;

  // If server returned existing session and we already have a full client for it, switch
  if (data.existing && _termSessions[todoId] && _termSessions[todoId].sessionId === sessionId && _termSessions[todoId].term) {
    _showTerminalOverlay(todoId);
    return;
  }

  // Create new terminal instance
  const term = new Terminal({
    theme: { background: '#1a1b1e', foreground: '#e2e8f0', cursor: '#4f6ef7',
             selectionBackground: 'rgba(79,110,247,0.3)' },
    fontFamily: 'Menlo, Monaco, "Cascadia Code", monospace',
    fontSize: 11, lineHeight: 1.3, cursorBlink: true,
  });
  const fitAddon = new FitAddon.FitAddon();
  term.loadAddon(fitAddon);

  _termSessions[todoId] = { sessionId, term, fitAddon, ws: null, alive: true };

  _updateSpinnersInPlace();
  _showTerminalOverlay(todoId);

  // Connect WebSocket
  _connectTermWs(todoId, sessionId);
}

function _connectTermWs(todoId, sessionId) {
  const session = _termSessions[todoId];
  if (!session) return;

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(proto + '://' + location.host + '/api/terminal/' + sessionId + '/ws');
  ws.binaryType = 'arraybuffer';
  session.ws = ws;

  ws.onopen = () => {
    const dims = session.fitAddon.proposeDimensions();
    if (dims) ws.send(JSON.stringify({ type: 'resize', rows: dims.rows, cols: dims.cols }));
  };

  ws.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) {
      session.term.write(new Uint8Array(e.data));
    } else if (typeof e.data === 'string') {
      try {
        const msg = JSON.parse(e.data);
        if (msg.type === 'error') session.term.writeln('\\r\\n\\x1b[31m' + msg.msg + '\\x1b[0m');
      } catch {}
    }
  };

  ws.onclose = () => {
    // Don't mark dead — PTY may still be alive for reconnection
  };

  ws.onerror = () => {};

  // Keystrokes → WS
  session.term.onData((data) => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(new TextEncoder().encode(data));
    }
  });

  session.term.onBinary((data) => {
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(Uint8Array.from(data, c => c.charCodeAt(0)));
    }
  });
}

function _showTerminalOverlay(todoId) {
  const session = _termSessions[todoId];
  if (!session || !session.term) return;

  _activeTermTodoId = todoId;
  const todo = allTodos.find(t => t.id === todoId);
  document.getElementById('terminal-title').textContent = todo ? todo.title : todoId;

  const overlay = document.getElementById('terminal-overlay');
  overlay.style.display = 'flex';
  document.body.style.overflow = 'hidden';
  requestAnimationFrame(() => overlay.classList.add('visible'));

  // Mount this session's terminal into the container
  const container = document.getElementById('terminal-container');
  container.innerHTML = '';
  if (session.term.element) {
    // Already opened — just re-attach the existing DOM element
    container.appendChild(session.term.element);
  } else {
    session.term.open(container);
  }
  session.fitAddon.fit();
  session.term.focus();

  // Resize observer + window resize for horizontal/vertical reflow
  if (!_termResizeObserver) {
    let _fitTimer = null;
    const doFit = () => {
      if (_fitTimer) clearTimeout(_fitTimer);
      _fitTimer = setTimeout(() => {
        if (!_activeTermTodoId || !_termSessions[_activeTermTodoId]) return;
        const s = _termSessions[_activeTermTodoId];
        if (!s.fitAddon) return;
        s.fitAddon.fit();
        if (s.ws && s.ws.readyState === WebSocket.OPEN) {
          const dims = s.fitAddon.proposeDimensions();
          if (dims) {
            console.log('[terminal] resize:', dims.cols, 'x', dims.rows);
            s.ws.send(JSON.stringify({ type: 'resize', rows: dims.rows, cols: dims.cols }));
          }
        }
      }, 50);
    };
    _termResizeObserver = new ResizeObserver(doFit);
    window.addEventListener('resize', doFit);
  }
  _termResizeObserver.observe(container);
}

function _startTermResize(e) {
  e.preventDefault();
  const panel = document.getElementById('terminal-panel');
  const startY = e.clientY;
  const startH = panel.offsetHeight;
  let _dragFitTimer = null;
  const fitDuringDrag = () => {
    if (_dragFitTimer) return;
    _dragFitTimer = setTimeout(() => {
      _dragFitTimer = null;
      if (!_activeTermTodoId || !_termSessions[_activeTermTodoId]) return;
      const s = _termSessions[_activeTermTodoId];
      if (!s.fitAddon) return;
      s.fitAddon.fit();
      if (s.ws && s.ws.readyState === WebSocket.OPEN) {
        const dims = s.fitAddon.proposeDimensions();
        if (dims) s.ws.send(JSON.stringify({ type: 'resize', rows: dims.rows, cols: dims.cols }));
      }
    }, 50);
  };
  function onMove(e) {
    const h = Math.min(window.innerHeight * 0.9, Math.max(150, startH - (e.clientY - startY)));
    panel.style.height = h + 'px';
    fitDuringDrag();
  }
  function onUp() {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    if (_dragFitTimer) { clearTimeout(_dragFitTimer); _dragFitTimer = null; }
    fitDuringDrag();
  }
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
}

function termSendCommand(cmd) {
  if (!_activeTermTodoId) return;
  const session = _termSessions[_activeTermTodoId];
  if (!session || !session.ws || session.ws.readyState !== WebSocket.OPEN) return;
  session.ws.send(new TextEncoder().encode(cmd + '\r'));
  session.term.focus();
}

function copyTmuxAttach() {
  if (!_activeTermTodoId) return;
  const session = _termSessions[_activeTermTodoId];
  if (!session) return;
  const cmd = 'tmux attach -t t-' + session.sessionId;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(cmd).then(() => showToast('Copied: ' + cmd)).catch(() => { _fallbackCopy(cmd); showToast('Copied: ' + cmd); });
  } else {
    _fallbackCopy(cmd); showToast('Copied: ' + cmd);
  }
}
function _fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.cssText = 'position:fixed;opacity:0';
  document.body.appendChild(ta); ta.select();
  document.execCommand('copy'); ta.remove();
}

function minimizeTerminal() {
  const overlay = document.getElementById('terminal-overlay');
  overlay.classList.remove('visible');
  document.body.style.overflow = '';
  setTimeout(() => { overlay.style.display = 'none'; }, 100);
  if (_termResizeObserver) _termResizeObserver.disconnect();
  _activeTermTodoId = null;
}

async function killTerminal(todoId, skipMinimize) {
  if (!todoId) return;
  const session = _termSessions[todoId];
  if (!session) return;

  // Kill on server
  try {
    await fetch('/api/terminal/' + session.sessionId + '/kill', { method: 'POST', headers: {'Content-Type': 'application/json'} });
  } catch {}

  // Clean up client
  if (session.ws) { try { session.ws.close(); } catch {} }
  session.alive = false;

  if (!skipMinimize && _activeTermTodoId === todoId) {
    minimizeTerminal();
  }
  delete _termSessions[todoId];
  _updateSpinnersInPlace();
}

function showShortcuts() {
  document.getElementById('shortcuts-overlay').classList.add('visible');
}
function hideShortcuts() {
  document.getElementById('shortcuts-overlay').classList.remove('visible');
}

let _settingsProviders = {}; // name -> {type, api_key, model, base_url}

async function showSettings() {
  try {
    const res = await fetch('/api/config');
    const config = await res.json();
    // Build providers from config
    _settingsProviders = {};
    if (config.providers) {
      for (const [name, prov] of Object.entries(config.providers)) {
        _settingsProviders[name] = { ...prov };
      }
    }
    // Migration: if no providers dict but legacy keys exist, show them
    if (Object.keys(_settingsProviders).length === 0) {
      if (config.anthropic_api_key) {
        _settingsProviders['anthropic'] = { type: 'anthropic', api_key: config.anthropic_api_key, model: config.model || 'claude-sonnet-4-20250514' };
      }
      const oai = config.openai_compat || {};
      if (oai.base_url) {
        _settingsProviders['openai-compat'] = { type: 'openai_compat', base_url: oai.base_url, api_key: oai.api_key || '', model: oai.model || '' };
      }
    }
    _renderProviderList();
    // Set active provider dropdown
    const sel = document.getElementById('settings-active-provider');
    _rebuildActiveDropdown();
    sel.value = config.active_provider || config._active_provider_name || '';
    document.getElementById('settings-subagents').checked = config.subagents_enabled !== false;
    document.getElementById('settings-max-subagents').value = config.max_subagents || 10;
  } catch {}
  _loadMcpStatus();
  _loadGitLog();
  document.getElementById('settings-overlay').classList.add('visible');
}

let _mcpStatusData = null;

async function _loadMcpStatus() {
  const container = document.getElementById('mcp-server-list');
  if (!container) return;
  try {
    const res = await fetch('/api/mcp/status');
    const data = await res.json();
    _mcpStatusData = data;
    // Set global auto-approve checkbox
    const aaChk = document.getElementById('mcp-auto-approve-all');
    if (aaChk) aaChk.checked = !!data.auto_approve_all;
    if (!data.servers || data.servers.length === 0) {
      container.innerHTML = '<div style="font-size:0.75rem;color:var(--subtle)">No MCP servers configured.</div>';
      return;
    }
    container.innerHTML = data.servers.map(s => {
      const dot = s.enabled ? (s.connected ? 'connected' : 'disconnected') : 'disabled';
      const toggleCls = s.enabled ? 'on' : 'off';
      const info = !s.enabled ? 'disabled' : (s.connected ? s.tool_count + ' tools' : esc(s.error || 'disconnected'));
      const expandId = 'mcp-tools-' + s.name;
      let html = '<div class="mcp-server-row">'
        + '<button class="mcp-server-toggle ' + toggleCls + '" onclick="_toggleMcpServer(\'' + esc(s.name) + '\',' + !s.enabled + ')"></button>'
        + '<span class="mcp-dot ' + dot + '"></span>'
        + '<strong style="flex-shrink:0">' + esc(s.label || s.name) + '</strong>'
        + '<span style="color:var(--subtle);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(info) + '</span>';
      const hasCredFields = s.credential_fields && s.credential_fields.length > 0;
      const hasOAuth = s.oauth_providers && s.oauth_providers.length > 0;
      if (hasCredFields || hasOAuth) {
        const allSet = s.bearer_connected || (hasCredFields && s.credential_fields.every(f => f.has_value));
        html += '<button class="mcp-tool-btn" onclick="_toggleMcpPanel(\'' + esc(s.name) + '\',\'config\')" style="font-size:0.65rem;' + (allSet ? '' : 'color:#f59e0b;border-color:#f59e0b') + '">Config</button>';
      }
      if (s.account_fields && s.account_fields.length > 0) {
        const acctLabel = s.account_count > 0 ? 'Accounts (' + s.account_count + ')' : 'Accounts';
        html += '<button class="mcp-tool-btn" onclick="_toggleMcpPanel(\'' + esc(s.name) + '\',\'accounts\')" style="font-size:0.65rem;' + (s.account_count === 0 ? 'color:#f59e0b;border-color:#f59e0b' : '') + '">' + acctLabel + '</button>';
      }
      if (s.enabled && s.connected) {
        html += '<button class="mcp-tool-btn" onclick="_toggleMcpPanel(\'' + esc(s.name) + '\',\'tools\')" style="font-size:0.65rem">Tools</button>';
      }
      html += '</div>';
      // Config panel (credentials)
      html += '<div id="mcp-config-' + s.name + '" class="mcp-tools-list" style="display:none"></div>';
      // Accounts panel
      html += '<div id="mcp-accounts-' + s.name + '" class="mcp-tools-list" style="display:none"></div>';
      // Tools panel
      if (s.enabled && s.connected) {
        html += '<div id="' + expandId + '" class="mcp-tools-list" style="display:none">Loading...</div>';
      }
      return html;
    }).join('');
    // Load tool lists for connected servers
    data.servers.filter(s => s.enabled && s.connected).forEach(s => _renderMcpTools(s));
  } catch {
    container.innerHTML = '<div style="font-size:0.75rem;color:var(--subtle)">Failed to load MCP status.</div>';
  }
}

function _renderMcpTools(server) {
  const el = document.getElementById('mcp-tools-' + server.name);
  if (!el) return;
  const tools = server.tools || [];
  const disabled = new Set(server.disabled_tools || []);
  const autoApproved = new Set(server.auto_approved_tools || []);
  if (tools.length === 0) {
    el.innerHTML = '<div style="color:var(--subtle)">No tools available.</div>';
    return;
  }
  el.innerHTML = tools.map(tool => {
    const t = typeof tool === 'string' ? tool : tool.name;
    const isDis = disabled.has(t);
    const isAuto = autoApproved.has(t);
    const sn = esc(server.name);
    const tn = esc(t);
    return '<div class="mcp-tool-row" style="' + (isDis ? 'opacity:0.5' : '') + '">'
      + '<span class="tool-name">' + tn + '</span>'
      + '<button class="mcp-tool-btn' + (isAuto ? ' active' : '') + '" onclick="_setMcpToolInline(this,\'' + sn + '\',\'' + tn + '\',null,' + !isAuto + ')" title="Auto-approve this tool">Auto</button>'
      + '<button class="mcp-tool-btn danger' + (isDis ? ' active' : '') + '" onclick="_setMcpToolInline(this,\'' + sn + '\',\'' + tn + '\',' + !isDis + ',null)" title="Disable this tool">Off</button>'
      + '</div>';
  }).join('');
}

function _toggleMcpPanel(serverName, panel) {
  const panels = ['config', 'accounts', 'tools'];
  for (const p of panels) {
    const el = document.getElementById('mcp-' + p + '-' + serverName);
    if (!el) continue;
    if (p === panel) {
      const show = el.style.display === 'none';
      el.style.display = show ? 'block' : 'none';
      if (show) {
        if (p === 'config') _renderMcpConfig(serverName);
        if (p === 'accounts') _loadMcpAccounts(serverName);
      }
    } else {
      el.style.display = 'none';
    }
  }
}

function _renderMcpConfig(serverName) {
  const el = document.getElementById('mcp-config-' + serverName);
  if (!el || !_mcpStatusData) return;
  const server = _mcpStatusData.servers.find(s => s.name === serverName);
  if (!server) return;
  const fields = server.credential_fields || [];
  const oauthProviders = server.oauth_providers || [];
  if (fields.length === 0 && oauthProviders.length === 0) {
    el.innerHTML = '<div style="color:var(--subtle)">No configuration needed.</div>';
    return;
  }
  let html = '';
  // OAuth connect buttons
  if (oauthProviders.length > 0) {
    oauthProviders.forEach(p => {
      const connected = server.bearer_connected || fields.some(f => f.has_value);
      html += '<button onclick="_startOAuth(\'' + esc(serverName) + '\',\'' + esc(p.id) + '\',\'\')" style="background:#4285f4;color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:0.78rem;margin-bottom:10px;width:100%">'
        + (connected ? 'Reconnect with ' : 'Connect with ') + esc(p.label) + '</button>';
    });
    if (server.bearer_connected || fields.some(f => f.has_value)) {
      html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px"><span style="font-size:0.7rem;color:#22c55e">Connected</span>'
        + '<button class="mcp-tool-btn danger" onclick="_clearMcpCredential(\'' + esc(serverName) + '\')" style="font-size:0.65rem">Disconnect</button></div>';
    }
    if (fields.length > 0) {
      html += '<details style="margin-bottom:8px"><summary style="font-size:0.72rem;color:var(--subtle);cursor:pointer">Or enter token manually</summary><div style="margin-top:6px">';
    }
  }
  if (fields.length > 0) {
    html += fields.map(f => {
      const fid = 'mcp-cred-' + serverName + '-' + f.key;
      return '<div style="margin-bottom:8px">'
        + '<label for="' + fid + '" style="font-size:0.72rem;color:var(--subtle);display:block;margin-bottom:2px">' + esc(f.label || f.key) + '</label>'
        + '<input id="' + fid + '" type="' + (f.type === 'password' ? 'password' : 'text') + '" '
        + 'placeholder="' + (f.has_value ? '(saved)' : 'Not set') + '" '
        + 'style="width:100%;padding:4px 8px;font-size:0.78rem;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg);box-sizing:border-box">'
        + '</div>';
    }).join('')
      + '<button onclick="_saveMcpConfig(\'' + esc(serverName) + '\')" style="background:var(--accent);color:#fff;border:none;padding:4px 14px;border-radius:6px;cursor:pointer;font-size:0.75rem">Save</button>';
    if (oauthProviders.length > 0) {
      html += '</div></details>';
    }
  }
  el.innerHTML = html;
}

async function _clearMcpCredential(serverName) {
  if (!confirm('Disconnect ' + serverName + '?')) return;
  if (!_mcpStatusData) return;
  const server = _mcpStatusData.servers.find(s => s.name === serverName);
  if (!server) return;
  const tokens = {};
  for (const f of (server.credential_fields || [])) {
    tokens[f.key] = '';
  }
  if (server.bearer_token_key) {
    tokens[server.bearer_token_key] = '';
  }
  _mcpAction(async () => {
    await fetch('/api/config', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tokens})
    });
    showToast(serverName + ' disconnected');
    await _refreshMcp();
  });
}

async function _saveMcpConfig(serverName) {
  if (!_mcpStatusData) return;
  const server = _mcpStatusData.servers.find(s => s.name === serverName);
  if (!server) return;
  const tokens = {};
  let hasChange = false;
  for (const f of (server.credential_fields || [])) {
    const input = document.getElementById('mcp-cred-' + serverName + '-' + f.key);
    if (input && input.value.trim()) {
      tokens[f.key] = input.value.trim();
      hasChange = true;
    }
  }
  if (!hasChange) { showToast('No changes to save'); return; }
  _mcpAction(async () => {
    await fetch('/api/config', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tokens})
    });
    showToast('Credentials saved');
    const configEl = document.getElementById('mcp-config-' + serverName);
    if (configEl) configEl.style.display = 'none';
    await _refreshMcp();
  });
}

async function _loadMcpAccounts(serverName) {
  const el = document.getElementById('mcp-accounts-' + serverName);
  if (!el || !_mcpStatusData) return;
  const server = _mcpStatusData.servers.find(s => s.name === serverName);
  if (!server) return;
  const fields = server.account_fields || [];
  try {
    const res = await fetch('/api/mcp/accounts/' + serverName);
    const data = await res.json();
    const accounts = data.accounts || [];
    let html = '';
    const oauthProviders = server.oauth_providers || [];
    // Existing accounts
    accounts.forEach(acct => {
      const isOAuth = acct.config.auth_type === 'oauth';
      const hasToken = !!acct.config.oauth_connected;
      html += '<div style="border:1px solid var(--border);border-radius:6px;padding:8px;margin-bottom:8px">';
      html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">';
      html += '<strong style="font-size:0.78rem;flex:1">' + esc(acct.config.name || acct.config.user || acct.id.slice(0,8)) + '</strong>';
      if (isOAuth) {
        const statusColor = hasToken ? '#22c55e' : '#f59e0b';
        const statusText = hasToken ? 'Connected' : 'Not connected';
        html += '<span style="font-size:0.65rem;color:' + statusColor + '">' + statusText + '</span>';
        // Find the OAuth provider for reconnect
        const prov = oauthProviders[0];
        if (prov) {
          html += '<button class="mcp-tool-btn" onclick="_startOAuth(\'' + esc(serverName) + '\',\'' + esc(prov.id) + '\',\'' + esc(acct.id) + '\')" style="font-size:0.65rem">' + (hasToken ? 'Reconnect' : 'Connect') + '</button>';
        }
      }
      html += '<button class="mcp-tool-btn danger" onclick="_deleteMcpAccount(\'' + esc(serverName) + '\',\'' + esc(acct.id) + '\')">Delete</button>';
      html += '</div>';
      if (!isOAuth) {
        fields.forEach(f => {
          if (f.key === 'name') return;
          const val = acct.config[f.key];
          const display = f.type === 'password' ? (val ? '***' : 'Not set') : (val != null ? String(val) : '');
          html += '<div style="font-size:0.72rem;color:var(--subtle);padding:1px 0"><span style="color:var(--muted)">' + esc(f.label) + ':</span> ' + esc(display) + '</div>';
        });
      } else {
        if (acct.config.url) html += '<div style="font-size:0.72rem;color:var(--subtle);padding:1px 0">' + esc(acct.config.url) + '</div>';
      }
      html += '</div>';
    });
    // OAuth quick-connect buttons
    if (oauthProviders.length > 0) {
      oauthProviders.forEach(p => {
        html += '<button onclick="_startOAuth(\'' + esc(serverName) + '\',\'' + esc(p.id) + '\',\'\')" style="background:#4285f4;color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:0.78rem;margin-bottom:8px;width:100%">Connect with ' + esc(p.label) + '</button>';
      });
    }
    // Manual add form (for non-OAuth)
    if (fields.length > 0) {
      html += '<details style="margin-top:4px"><summary style="font-size:0.72rem;color:var(--subtle);cursor:pointer">Add manually</summary>';
      html += '<div style="border:1px dashed var(--border);border-radius:6px;padding:8px;margin-top:4px">';
      fields.forEach(f => {
        const fid = 'mcp-acct-' + serverName + '-' + f.key;
        if (f.type === 'boolean') {
          html += '<label style="display:flex;align-items:center;gap:6px;font-size:0.72rem;margin-bottom:4px;cursor:pointer">';
          html += '<input id="' + fid + '" type="checkbox"' + (f.default ? ' checked' : '') + '>';
          html += esc(f.label) + '</label>';
        } else if (f.type === 'select') {
          html += '<label style="font-size:0.72rem;color:var(--subtle);display:block;margin-bottom:2px">' + esc(f.label) + '</label>';
          html += '<select id="' + fid + '" style="width:100%;padding:4px 8px;font-size:0.78rem;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg);margin-bottom:6px;box-sizing:border-box">';
          (f.options || []).forEach(o => { html += '<option' + (o === f.default ? ' selected' : '') + '>' + esc(o) + '</option>'; });
          html += '</select>';
        } else {
          html += '<label style="font-size:0.72rem;color:var(--subtle);display:block;margin-bottom:2px">' + esc(f.label) + '</label>';
          html += '<input id="' + fid + '" type="' + (f.type === 'password' ? 'password' : f.type === 'number' ? 'number' : 'text') + '"';
          if (f.placeholder) html += ' placeholder="' + esc(f.placeholder) + '"';
          if (f.default != null && f.type === 'number') html += ' value="' + f.default + '"';
          html += ' style="width:100%;padding:4px 8px;font-size:0.78rem;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--fg);margin-bottom:6px;box-sizing:border-box">';
        }
      });
      html += '<button onclick="_addMcpAccount(\'' + esc(serverName) + '\')" style="background:var(--accent);color:#fff;border:none;padding:4px 14px;border-radius:6px;cursor:pointer;font-size:0.75rem">Add</button>';
      html += '</div></details>';
    }
    el.innerHTML = html;
  } catch { el.innerHTML = '<div style="color:var(--subtle)">Failed to load accounts.</div>'; }
}

async function _addMcpAccount(serverName) {
  if (!_mcpStatusData) return;
  const server = _mcpStatusData.servers.find(s => s.name === serverName);
  if (!server) return;
  const config = {};
  for (const f of (server.account_fields || [])) {
    const el = document.getElementById('mcp-acct-' + serverName + '-' + f.key);
    if (!el) continue;
    if (f.type === 'boolean') config[f.key] = el.checked;
    else if (f.type === 'number') config[f.key] = parseInt(el.value) || f.default || 0;
    else config[f.key] = el.value.trim();
  }
  if (!config.name && !config.user) { showToast('Name is required', true); return; }
  _mcpAction(async () => {
    const res = await fetch('/api/mcp/accounts/' + serverName, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(config)
    });
    if (res.ok) {
      showToast('Account added');
      await _loadMcpStatus();
      _toggleMcpPanel(serverName, 'accounts');
    } else {
      const data = await res.json();
      showToast(data.error || 'Failed', true);
    }
  });
}

async function _startOAuth(serverName, providerId, accountId) {
  try {
    const params = new URLSearchParams({server: serverName, provider: providerId});
    if (accountId) params.set('account_id', accountId);
    const res = await fetch('/api/mcp/oauth/start?' + params);
    const data = await res.json();
    if (data.auth_url) {
      window.open(data.auth_url, 'oauth', 'width=600,height=700');
    } else {
      showToast(data.error || 'Failed to start OAuth', true);
    }
  } catch { showToast('Failed to start OAuth', true); }
}

// Listen for OAuth completion from popup
window.addEventListener('message', async (e) => {
  if (e.data && e.data.type === 'oauth_complete') {
    showToast('Account connected');
    _mcpLoading(true);
    await _loadMcpStatus();
    _mcpLoading(false);
    // Re-open panels that were open
    document.querySelectorAll('[id^="mcp-accounts-"]').forEach(el => {
      if (el.style.display !== 'none') {
        const name = el.id.replace('mcp-accounts-', '');
        _loadMcpAccounts(name);
      }
    });
  }
});

async function _deleteMcpAccount(serverName, accountId) {
  if (!confirm('Delete this account?')) return;
  _mcpAction(async () => {
    const res = await fetch('/api/mcp/accounts/' + serverName + '/' + accountId, { method: 'DELETE', headers: {'Content-Type': 'application/json'} });
    if (res.ok) {
      showToast('Account deleted');
      await _loadMcpStatus();
      _toggleMcpPanel(serverName, 'accounts');
    }
  });
}

function _mcpLoading(show) {
  const el = document.getElementById('mcp-loading');
  if (el) el.style.display = show ? 'flex' : 'none';
}

async function _mcpAction(fn) {
  _mcpLoading(true);
  try { await fn(); } finally { _mcpLoading(false); }
}

async function _toggleMcpServer(name, enabled) {
  _mcpAction(async () => {
    await fetch('/api/mcp/servers', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({server: name, enabled})
    });
    showToast(enabled ? name + ' enabled' : name + ' disabled');
    if (enabled) await new Promise(r => setTimeout(r, 3000));
    await _loadMcpStatus();
  });
}

async function _setMcpTool(server, tool, disabled, autoApproved) {
  const body = {server, tool};
  if (disabled !== null) body.disabled = disabled;
  if (autoApproved !== null) body.auto_approved = autoApproved;
  try {
    await fetch('/api/mcp/tools', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
  } catch { showToast('Failed to update tool', true); }
}

async function _setMcpToolInline(btn, server, tool, disabled, autoApproved) {
  // Toggle UI immediately without collapsing the panel
  btn.classList.toggle('active');
  if (disabled !== null) {
    const row = btn.closest('.mcp-tool-row');
    if (row) row.style.opacity = disabled ? '0.5' : '1';
  }
  await _setMcpTool(server, tool, disabled, autoApproved);
}

function _showToolApproval(data, todoId) {
  // Always store on session so it survives chat close/reopen
  const session = _chatSessions[todoId];
  if (session) {
    if (!session._activeApprovals) session._activeApprovals = {};
    session._activeApprovals[data.approval_id] = data;
  }
  // Show toast so user notices even if chat isn't open
  const streamEl = document.getElementById('chat-assistant-streaming');
  if (!streamEl) {
    showToast(data.server + '.' + data.tool_display + ' needs approval — open chat to respond');
    return;
  }
  _renderToolApprovalCard(data, streamEl);
}

function _renderToolApprovalCard(data, container) {
  const div = document.createElement('div');
  div.id = 'tool-approval-' + data.approval_id;
  div.style.cssText = 'background:rgba(79,110,247,0.08);border:1px solid var(--accent);border-radius:8px;padding:10px 12px;margin:6px 0;font-size:0.8rem';
  const argsStr = Object.entries(data.args || {}).map(([k,v]) => esc(k) + ': ' + esc(typeof v === 'string' ? v : JSON.stringify(v)).slice(0,80)).join('<br>');
  div.innerHTML = '<div style="font-weight:600;margin-bottom:6px">' + esc(data.server) + '.' + esc(data.tool_display) + ' wants to run</div>'
    + (argsStr ? '<div style="color:var(--subtle);font-size:0.75rem;margin-bottom:8px;font-family:monospace">' + argsStr + '</div>' : '')
    + '<div style="display:flex;gap:6px;align-items:center">'
    + '<button onclick="_respondToolApproval(\'' + esc(data.approval_id) + '\',true,false,this)" style="background:var(--accent);color:#fff;border:none;padding:4px 12px;border-radius:6px;cursor:pointer;font-size:0.75rem">Approve</button>'
    + '<button onclick="_respondToolApproval(\'' + esc(data.approval_id) + '\',true,true,this)" style="background:#22c55e;color:#fff;border:none;padding:4px 12px;border-radius:6px;cursor:pointer;font-size:0.75rem">Always Allow</button>'
    + '<button onclick="_respondToolApproval(\'' + esc(data.approval_id) + '\',false,false,this)" style="background:none;border:1px solid #ef4444;color:#ef4444;padding:4px 12px;border-radius:6px;cursor:pointer;font-size:0.75rem">Deny</button>'
    + '</div>';
  container.appendChild(div);
  const log = document.getElementById('chat-log');
  if (log) log.scrollTop = log.scrollHeight;
}

async function _respondToolApproval(approvalId, approved, alwaysAllow, btn) {
  const card = document.getElementById('tool-approval-' + approvalId);
  // Disable all buttons in the card
  if (card) {
    card.querySelectorAll('button').forEach(b => { b.disabled = true; b.style.opacity = '0.4'; });
    card.style.borderColor = approved ? '#22c55e' : '#ef4444';
    card.style.background = approved ? 'rgba(34,197,94,0.06)' : 'rgba(239,68,68,0.06)';
    const statusDiv = document.createElement('div');
    statusDiv.style.cssText = 'font-size:0.7rem;margin-top:4px';
    statusDiv.style.color = approved ? '#22c55e' : '#ef4444';
    statusDiv.textContent = approved ? (alwaysAllow ? 'Always allowed' : 'Approved') : 'Denied';
    card.appendChild(statusDiv);
  }
  // Remove from active approvals so it doesn't re-render on chat reopen
  if (_activeChatTodoId) {
    const session = _chatSessions[_activeChatTodoId];
    if (session && session._activeApprovals) {
      delete session._activeApprovals[approvalId];
    }
  }
  try {
    await fetch('/api/mcp/approve', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({approval_id: approvalId, approved, always_allow: alwaysAllow})
    });
  } catch { showToast('Failed to send approval', true); }
}

async function _toggleAutoApproveAll(checked) {
  try {
    await fetch('/api/config', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({auto_approve_all: checked})
    });
    showToast(checked ? 'Auto-approve enabled' : 'Auto-approve disabled');
  } catch { showToast('Failed to update', true); }
}

async function _logout() {
  if (!confirm('Log out?')) return;
  await fetch('/api/auth/logout', { method: 'POST', headers: {'Content-Type': 'application/json'} });
  window.location.reload();
}

async function _refreshMcp() {
  showToast('Reconnecting MCP servers...');
  _mcpLoading(true);
  try {
    await fetch('/api/mcp/reconnect', { method: 'POST', headers: {'Content-Type': 'application/json'} });
    await _loadMcpStatus();
    showToast('MCP reconnected');
  } catch {
    showToast('Failed to reconnect MCP', true);
  } finally {
    _mcpLoading(false);
  }
}

async function _loadGitLog() {
  const container = document.getElementById('git-log-list');
  if (!container) return;
  try {
    const res = await fetch('/api/git/log');
    const data = await res.json();
    if (data.error) {
      container.innerHTML = '<div style="font-size:0.75rem;color:var(--subtle)">' + esc(data.error) + '</div>';
      return;
    }
    if (!data.commits || data.commits.length === 0) {
      container.innerHTML = '<div style="font-size:0.75rem;color:var(--subtle)">No commits yet.</div>';
      return;
    }
    container.innerHTML = data.commits.map(c => {
      const date = c.date.replace(/\s\+.*/, '').replace('T', ' ').slice(0, 16);
      const hash = c.hash.slice(0, 8);
      return '<div style="display:flex;align-items:center;gap:6px;padding:4px 0;border-bottom:1px solid var(--border);font-size:0.75rem">'
        + '<code style="color:var(--subtle);flex-shrink:0">' + esc(hash) + '</code>'
        + '<span style="color:var(--subtle);flex-shrink:0;width:100px">' + esc(date) + '</span>'
        + '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(c.message) + '</span>'
        + '<button onclick="_gitRollback(\'' + esc(c.hash) + '\')" style="flex-shrink:0;background:none;border:1px solid var(--border);border-radius:4px;padding:1px 6px;font-size:0.65rem;cursor:pointer;color:var(--subtle)" onmousedown="event.stopPropagation()">Restore</button>'
        + '</div>';
    }).join('');
  } catch {
    container.innerHTML = '<div style="font-size:0.75rem;color:var(--subtle)">Failed to load git log.</div>';
  }
}

async function _gitCommit() {
  const message = prompt('Commit message:', 'Manual save ' + new Date().toLocaleString());
  if (message === null) return;
  try {
    const res = await fetch('/api/git/commit', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ message }),
    });
    const data = await res.json();
    if (data.ok) {
      showToast(data.message || 'Saved');
      _loadGitLog();
    } else {
      showToast(data.error || 'Commit failed', true);
    }
  } catch {
    showToast('Failed to commit', true);
  }
}

async function _gitRollback(hash) {
  if (!confirm('Restore todos to commit ' + hash.slice(0, 8) + '? Current changes will be overwritten.')) return;
  try {
    const res = await fetch('/api/git/rollback', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ hash }),
    });
    const data = await res.json();
    if (data.ok) {
      showToast('Restored to ' + hash.slice(0, 8));
      _loadGitLog();
      loadTodos();
    } else {
      showToast(data.error || 'Rollback failed', true);
    }
  } catch {
    showToast('Failed to rollback', true);
  }
}

function _rebuildActiveDropdown() {
  const sel = document.getElementById('settings-active-provider');
  const cur = sel.value;
  sel.innerHTML = '<option value="local">Local CLI (fallback)</option>';
  for (const name of Object.keys(_settingsProviders)) {
    const prov = _settingsProviders[name];
    const label = name + ' (' + (prov.type === 'anthropic' ? 'Anthropic' : 'OpenAI-compat') + ')';
    sel.innerHTML += '<option value="' + esc(name) + '">' + esc(label) + '</option>';
  }
  if (cur && [...sel.options].some(o => o.value === cur)) sel.value = cur;
}

function _renderProviderList() {
  const container = document.getElementById('provider-list');
  let html = '';
  for (const [name, prov] of Object.entries(_settingsProviders)) {
    const isAnthro = prov.type === 'anthropic';
    html += '<div style="border:1px solid var(--border);border-radius:8px;padding:10px;margin:8px 0">';
    html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px"><strong>' + esc(name) + '</strong>';
    html += '<span style="font-size:0.7rem;color:var(--subtle)">' + (isAnthro ? 'Anthropic' : 'OpenAI-compat') + '</span>';
    html += '<button onclick="_removeProvider(\'' + esc(name).replace(/'/g,"\\'") + '\')" style="margin-left:auto;background:none;border:none;color:var(--danger);cursor:pointer;font-size:0.75rem">Remove</button></div>';
    html += '<label style="font-size:0.75rem">API Key</label>';
    html += '<input type="password" data-prov="' + esc(name) + '" data-field="api_key" value="' + esc(prov.api_key || '') + '" placeholder="' + (isAnthro ? 'sk-ant-...' : 'API key') + '" style="width:100%;margin-bottom:4px;padding:4px 8px;border:1px solid var(--border);border-radius:4px;font-size:0.8rem">';
    if (!isAnthro) {
      html += '<label style="font-size:0.75rem">Base URL</label>';
      html += '<input type="text" data-prov="' + esc(name) + '" data-field="base_url" value="' + esc(prov.base_url || '') + '" placeholder="https://..." style="width:100%;margin-bottom:4px;padding:4px 8px;border:1px solid var(--border);border-radius:4px;font-size:0.8rem">';
    }
    html += '<label style="font-size:0.75rem">Model</label>';
    if (isAnthro) {
      html += '<select data-prov="' + esc(name) + '" data-field="model" style="width:100%;padding:4px 8px;border:1px solid var(--border);border-radius:4px;font-size:0.8rem">';
      for (const m of ['claude-sonnet-4-20250514','claude-haiku-4-5-20251001','claude-opus-4-20250514']) {
        html += '<option value="' + m + '"' + (prov.model === m ? ' selected' : '') + '>' + m.replace(/-20[0-9]+$/, '') + '</option>';
      }
      html += '</select>';
    } else {
      html += '<input type="text" data-prov="' + esc(name) + '" data-field="model" value="' + esc(prov.model || '') + '" placeholder="model-id" style="width:100%;padding:4px 8px;border:1px solid var(--border);border-radius:4px;font-size:0.8rem">';
    }
    html += '</div>';
  }
  container.innerHTML = html;
}

function _addProvider() {
  const name = prompt('Provider name (e.g. "runpod", "ollama"):');
  if (!name || _settingsProviders[name]) return;
  const type = prompt('Type: "anthropic" or "openai_compat":', 'openai_compat');
  if (type !== 'anthropic' && type !== 'openai_compat') return;
  _settingsProviders[name] = { type, api_key: '', model: '', base_url: type === 'openai_compat' ? '' : undefined };
  _renderProviderList();
  _rebuildActiveDropdown();
}

function _removeProvider(name) {
  delete _settingsProviders[name];
  _renderProviderList();
  _rebuildActiveDropdown();
}

function _onActiveProviderChange() {}

function hideSettings() {
  document.getElementById('settings-overlay').classList.remove('visible');
}

async function saveSettings() {
  // Read values from DOM back into _settingsProviders
  document.querySelectorAll('#provider-list [data-prov]').forEach(el => {
    const name = el.dataset.prov;
    const field = el.dataset.field;
    if (_settingsProviders[name]) {
      const val = el.value.trim();
      if (field === 'api_key' && val.includes('...')) return; // Skip redacted
      _settingsProviders[name][field] = val;
    }
  });
  const activeProvider = document.getElementById('settings-active-provider').value;
  const body = {
    providers: _settingsProviders,
    active_provider: activeProvider,
    subagents_enabled: document.getElementById('settings-subagents').checked,
    max_subagents: parseInt(document.getElementById('settings-max-subagents').value) || 10,
  };
  try {
    await fetch('/api/config', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    hideSettings();
    showToast('Settings saved');
  } catch {
    showToast('Failed to save settings');
  }
}

async function resumeConv(convId, todoId) {
  // Open chat panel with this conversation
  if (todoId) {
    openChat(todoId, convId);
  }
}

// ---------------------------------------------------------------------------
// Chat UI
// ---------------------------------------------------------------------------

async function startChatBackground(todoId) {
  // Start /ea workon in background without opening the chat panel
  await _loadChatSession(todoId);
  let session = _chatSessions[todoId];
  const isNew = !session || session.messages.length === 0;
  if (!session) {
    _chatSessions[todoId] = { conversationId: null, messages: [], streamingText: '', streamingJobId: null };
    session = _chatSessions[todoId];
  }
  if (!isNew) {
    // Session exists — send checkon instead
    if (session.streamingJobId) { showToast('Chat is busy'); return; }
    const msg = '/ea checkon ' + todoId;
    session.messages.push({ role: 'user', content: msg });
    session.streamingJobId = 'pending';
    session.streamingText = '';
    _updateSpinnersInPlace();
    if (_activeChatTodoId === todoId) { _syncChatSendBtn(todoId); _renderChatLog(todoId); }
    showToast('Checking on...');
    try {
      const res = await fetch(API + '/' + todoId + '/chat', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ message: msg }),
      });
      if (!res.ok) { _chatStreamDone(todoId, 'Failed to send'); return; }
      const data = await res.json();
      _streamChatResponse(todoId, data.job_id);
    } catch (e) {
      _chatStreamDone(todoId, 'Network error');
    }
    return;
  }
  const msg = '/ea workon ' + todoId;
  session.messages.push({ role: 'user', content: msg });
  session.streamingJobId = 'pending';
  session.streamingText = '';
  _updateSpinnersInPlace();
  try {
    const res = await fetch(API + '/' + todoId + '/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ message: msg }),
    });
    if (!res.ok) { _chatStreamDone(todoId, 'Failed to send'); return; }
    const data = await res.json();
    _streamChatResponse(todoId, data.job_id);
  } catch (e) {
    _chatStreamDone(todoId, 'Network error');
  }
}

async function openChat(todoId, conversationId) {
  // Defer title updated mark-read until navigating away
  _pendingMarkRead = todoId;
  // Load persisted chat from server
  await _loadChatSession(todoId);
  let session = _chatSessions[todoId];
  const isNew = !session || session.messages.length === 0;
  if (!session) {
    _chatSessions[todoId] = { conversationId: conversationId || null, messages: [], streamingText: '', streamingJobId: null };
    session = _chatSessions[todoId];
  } else if (conversationId) {
    session.conversationId = conversationId;
  }
  _showChatOverlay(todoId);
  // Auto-send /ea workon for brand-new chats (no conversationId = not resuming)
  if (isNew && !conversationId) {
    const msg = '/ea workon ' + todoId;
    session.messages.push({ role: 'user', content: msg });
    session.streamingJobId = 'pending';
    session.streamingText = '';
    _syncChatSendBtn(todoId);
    _renderChatLog(todoId);
    try {
      const res = await fetch(API + '/' + todoId + '/chat', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ message: msg }),
      });
      if (!res.ok) { _chatStreamDone(todoId, 'Failed to send'); return; }
      const data = await res.json();
      _streamChatResponse(todoId, data.job_id);
    } catch (e) {
      _chatStreamDone(todoId, 'Network error');
    }
  }
}

function _showChatOverlay(todoId) {
  _activeChatTodoId = todoId;
  const todo = allTodos.find(t => t.id === todoId);
  const title = todo ? _parseTitle(todo.title || '').displayTitle : todoId;
  document.getElementById('chat-title').textContent = title;
  _updateProviderBadge();

  const overlay = document.getElementById('chat-overlay');
  overlay.style.display = 'flex';
  document.body.style.overflow = 'hidden';
  requestAnimationFrame(() => overlay.classList.add('visible'));

  _renderChatLog(todoId);
  const input = document.getElementById('chat-input');
  input.value = '';
  input.focus();
  _syncChatSendBtn(todoId);
}

async function _updateProviderBadge() {
  const badge = document.getElementById('chat-provider-badge');
  if (!badge) return;
  try {
    const res = await fetch('/api/config');
    const config = await res.json();
    const name = config._active_provider_name || 'local';
    const type = config._active_provider_type || 'local';
    const providers = config.providers || {};
    const prov = providers[name] || {};
    let label = name;
    if (type === 'anthropic') label = (prov.model || 'claude').replace(/-20[0-9]+$/, '');
    else if (type === 'openai_compat') label = name + ': ' + (prov.model || 'default');
    badge.textContent = label;
  } catch {
    badge.textContent = '';
  }
}

function _syncChatSendBtn(todoId) {
  if (_activeChatTodoId !== todoId) return;
  const session = _chatSessions[todoId];
  const isStreaming = !!(session && session.streamingJobId);
  const btn = document.getElementById('chat-send-btn');
  if (isStreaming) {
    btn.textContent = 'Stop';
    btn.disabled = false;
    btn.classList.add('chat-stop-mode');
  } else {
    btn.textContent = 'Send';
    btn.disabled = false;
    btn.classList.remove('chat-stop-mode');
  }
}

function _renderChatLog(todoId) {
  const session = _chatSessions[todoId];
  if (!session) return;
  const log = document.getElementById('chat-log');
  let html = '';
  session.messages.forEach((msg, i) => {
    if (i > 0 && msg.role === 'user') html += '<hr class="chat-turn-sep">';
    if (msg.role === 'user') {
      html += '<div class="chat-user-line">&gt; ' + esc(msg.content) + '</div>';
    } else {
      html += '<div class="chat-assistant-block">' + renderMd(msg.content) + '</div>';
    }
  });
  // If currently streaming, re-attach the streaming area + spinner (no extra separator —
  // the user message that triggered this is already the last rendered item)
  if (session.streamingJobId) {
    html += '<div id="chat-assistant-streaming"></div>';
    html += '<div id="chat-spinner" style="margin-top:4px"><l-bouncy size="20" speed="1.75" color="var(--accent)"></l-bouncy></div>';
  }
  log.innerHTML = html;
  // If streaming, populate the streaming div with current partial text + pending approvals
  if (session.streamingJobId) {
    const streamEl = document.getElementById('chat-assistant-streaming');
    if (streamEl) {
      if (session.streamingText) {
        streamEl.innerHTML = '<div class="chat-assistant-block">' + renderMd(session.streamingText) + '</div>';
      }
      // Re-render any active (unanswered) approval cards
      if (session._activeApprovals) {
        for (const data of Object.values(session._activeApprovals)) {
          _renderToolApprovalCard(data, streamEl);
        }
      }
    }
  }
  log.scrollTop = log.scrollHeight;
}

async function _showChatHistory() {
  const todoId = _activeChatTodoId;
  if (!todoId) return;
  const log = document.getElementById('chat-log');
  if (!log) return;
  try {
    const res = await fetch('/api/chats/' + todoId + '/conversations');
    const data = await res.json();
    const convs = data.conversations || [];
    const currentNum = data.current != null ? data.current : (convs.length ? convs[0].num : 0);
    if (convs.length <= 1) { showToast('No previous conversations'); return; }
    let html = '<div style="padding:8px"><div style="font-size:0.82rem;font-weight:600;margin-bottom:8px;color:#e2e8f0">Conversations</div>';
    convs.forEach(c => {
      const date = new Date(c.started).toLocaleDateString(undefined, {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
      const isCurrent = c.num === currentNum;
      html += '<div style="display:flex;align-items:center;gap:8px;padding:6px 8px;border-radius:6px;cursor:pointer;margin-bottom:4px;'
        + (isCurrent ? 'background:rgba(79,110,247,0.15);border:1px solid var(--accent)' : 'background:rgba(255,255,255,0.05);border:1px solid transparent')
        + '" onclick="' + (isCurrent ? '_returnToCurrent(\'' + todoId + '\')' : '_loadConversation(\'' + todoId + '\',' + c.num + ')') + '">'
        + '<div style="flex:1"><div style="font-size:0.78rem;color:#e2e8f0">' + date + '</div>'
        + '<div style="font-size:0.68rem;color:var(--subtle)">' + c.message_count + ' messages</div></div>'
        + (isCurrent ? '<span style="font-size:0.65rem;color:var(--accent)">current</span>' : '')
        + '</div>';
    });
    html += '</div>';
    log.innerHTML = html;
  } catch { showToast('Failed to load history', true); }
}

async function _loadConversation(todoId, convNum) {
  try {
    const res = await fetch('/api/chats/' + todoId + '/conversations/' + convNum);
    const data = await res.json();
    const log = document.getElementById('chat-log');
    if (!log) return;
    // Track that we're viewing a historical conversation
    const session = _chatSessions[todoId];
    if (session) session._viewingConvNum = convNum;
    // Show floating banner over chat log
    _showHistoryBanner(todoId);
    let html = '';
    (data.messages || []).forEach((msg, i) => {
      if (i > 0 && msg.role === 'user') html += '<hr class="chat-turn-sep">';
      if (msg.role === 'user') {
        html += '<div class="chat-user-line">&gt; ' + esc(msg.content) + '</div>';
      } else {
        html += '<div class="chat-assistant-block">' + renderMd(msg.content) + '</div>';
      }
    });
    if (!data.messages || data.messages.length === 0) {
      html += '<div style="color:var(--subtle);padding:8px">No messages in this conversation.</div>';
    }
    log.innerHTML = html;
  } catch { showToast('Failed to load conversation', true); }
}

async function restartChat() {
  const todoId = _activeChatTodoId;
  if (!todoId) return;
  // Stop any running job first
  const session = _chatSessions[todoId];
  if (session && session.streamingJobId && session.streamingJobId !== 'pending') {
    try { await fetch('/api/jobs/' + session.streamingJobId + '/kill', { method: 'POST', headers: {'Content-Type': 'application/json'} }); } catch {}
  }
  _chatSessions[todoId] = { conversationId: null, messages: [], streamingText: '', streamingJobId: null };
  _renderChatLog(todoId);
  _syncChatSendBtn(todoId);
  fetch('/api/chats/' + todoId, { method: 'DELETE', headers: {'Content-Type': 'application/json'} }).catch(() => {});
  showToast('New conversation started');
}

function minimizeChat() {
  const overlay = document.getElementById('chat-overlay');
  overlay.classList.remove('visible');
  document.body.style.overflow = '';
  setTimeout(() => { overlay.style.display = 'none'; }, 100);
  _activeChatTodoId = null;
}

let _stopConfirmTimer = null;
let _stopConfirmTodoId = null;

function chatSendOrStop(todoId) {
  const session = _chatSessions[todoId];
  if (session && session.streamingJobId) {
    // Require double-press to stop
    if (_stopConfirmTodoId === todoId && _stopConfirmTimer) {
      clearTimeout(_stopConfirmTimer);
      _stopConfirmTimer = null;
      _stopConfirmTodoId = null;
      stopChat(todoId);
      _syncChatSendBtn(todoId);
    } else {
      _stopConfirmTodoId = todoId;
      const btn = document.getElementById('chat-send-btn');
      if (btn) { btn.textContent = 'Confirm Stop'; }
      _stopConfirmTimer = setTimeout(() => {
        _stopConfirmTimer = null;
        _stopConfirmTodoId = null;
        _syncChatSendBtn(todoId);
      }, 2000);
    }
  } else {
    sendChatMessage(todoId);
  }
}

function _showHistoryBanner(todoId) {
  _hideHistoryBanner();
  const log = document.getElementById('chat-log');
  if (!log) return;
  const banner = document.createElement('div');
  banner.id = 'chat-history-banner';
  banner.style.cssText = 'display:flex;align-items:center;gap:8px;padding:5px 14px;background:rgba(79,110,247,0.12);border-bottom:1px solid rgba(79,110,247,0.3);font-size:0.72rem;flex-shrink:0';
  banner.innerHTML = '<span style="cursor:pointer;color:var(--accent)" onclick="_returnToCurrent(\'' + todoId + '\')">&larr; Back to current</span>'
    + '<span style="color:var(--subtle)">Viewing past conversation — type to resume</span>';
  log.parentElement.insertBefore(banner, log);
}

function _hideHistoryBanner() {
  const banner = document.getElementById('chat-history-banner');
  if (banner) banner.remove();
}

async function _returnToCurrent(todoId) {
  const session = _chatSessions[todoId];
  if (session) delete session._viewingConvNum;
  _hideHistoryBanner();
  await _loadChatSession(todoId);
  _renderChatLog(todoId);
}

async function sendChatMessage(todoId) {
  const session = _chatSessions[todoId];
  if (session && session.streamingJobId) return;
  const input = document.getElementById('chat-input');
  const message = input.value.trim();
  if (!message) return;
  input.value = '';
  let resumeConv = null;
  if (session && session._viewingConvNum != null) {
    resumeConv = session._viewingConvNum;
    delete session._viewingConvNum;
  }
  await _sendChatDirect(todoId, message, resumeConv);
}

async function _sendChatDirect(todoId, message, resumeConv) {
  const session = _chatSessions[todoId];
  if (!session) return;

  // Add user message and mark as streaming (spinner will show via _renderChatLog)
  session.messages.push({ role: 'user', content: message });
  session.streamingJobId = 'pending';
  session.streamingText = '';

  if (_activeChatTodoId === todoId) {
    _syncChatSendBtn(todoId);
    _renderChatLog(todoId);
  }

  // POST to start job
  try {
    const body = { message, conversation_id: session.conversationId };
    if (resumeConv != null) body.resume_conv = resumeConv;
    const res = await fetch(API + '/' + todoId + '/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      _chatStreamDone(todoId, 'Failed to send message');
      return;
    }
    const data = await res.json();
    // If we resumed a past conversation, reload session to get its full history
    if (resumeConv != null) {
      _hideHistoryBanner();
      await _loadChatSession(todoId);
      if (_activeChatTodoId === todoId) _renderChatLog(todoId);
    }
    _streamChatResponse(todoId, data.job_id);
  } catch (e) {
    _chatStreamDone(todoId, 'Network error');
  }
}

function _streamChatResponse(todoId, jobId) {
  console.log('[chat] _streamChatResponse called', todoId, jobId);
  const session = _chatSessions[todoId];
  if (!session) { console.log('[chat] no session for', todoId); return; }

  session.streamingJobId = jobId;
  session.streamingText = '';
  _updateSpinnersInPlace();

  const es = new EventSource('/api/jobs/' + jobId + '/stream');
  let textDiv = null;
  let currentBlockText = ''; // text for the current block only (resets after tool calls)

  // Check if this stream is still the active one for this todo
  function isStale() {
    const cur = _chatSessions[todoId];
    return !cur || cur.streamingJobId !== jobId;
  }

  console.log('[chat] SSE connected for job', jobId, 'todo', todoId);
  es.onmessage = function(e) {
    if (isStale()) { console.log('[chat] stale, closing'); es.close(); return; }

    let raw;
    try { raw = JSON.parse(e.data); } catch { console.log('[chat] parse error', e.data); return; }
    console.log('[chat]', typeof raw === 'string' ? raw : JSON.stringify(raw));

    if (typeof raw === 'object' && raw.__done__) {
      es.close();
      if (isStale()) return;
      const cur = _chatSessions[todoId];
      if (raw.conversation_id && cur) {
        cur.conversationId = raw.conversation_id;
      }
      _chatStreamDone(todoId, null, cur ? cur.streamingText : '');
      return;
    }

    if (typeof raw === 'object' && raw.__tool_approval__) {
      _showToolApproval(raw, todoId);
      return;
    }

    // --- Structured event handling (no string matching) ---
    if (typeof raw === 'object' && raw.__tool_call__) {
      const streamEl = document.getElementById('chat-assistant-streaming');
      if (streamEl) {
        const div = document.createElement('div');
        div.className = 'chat-tool-line';
        div.textContent = raw.subagent
          ? `[${raw.subagent}] \u25b6 ${raw.name}...`
          : `\u25b6 ${raw.name}...`;
        streamEl.appendChild(div);
        streamEl.scrollTop = streamEl.scrollHeight;
        textDiv = null;
        currentBlockText = '';
      }
      return;
    }

    if (typeof raw === 'object' && raw.__error__) {
      const streamEl = document.getElementById('chat-assistant-streaming');
      if (streamEl) {
        const div = document.createElement('div');
        div.style.cssText = 'color:#ef4444;font-size:0.8rem;padding:6px 10px;background:rgba(239,68,68,0.08);border-radius:6px;border-left:3px solid #ef4444;margin:4px 0;white-space:pre-wrap;word-break:break-word';
        div.textContent = raw.message;
        streamEl.appendChild(div);
        textDiv = null;
        currentBlockText = '';
      }
      return;
    }

    if (typeof raw === 'object' && raw.__status__) {
      const streamEl = document.getElementById('chat-assistant-streaming');
      if (streamEl) {
        const div = document.createElement('div');
        if (raw.__status__ === 'done') {
          div.className = 'chat-cost-line';
          div.textContent = raw.cost != null
            ? `\u2713 Done \u2014 $${raw.cost.toFixed(4)}`
            : `\u2713 Done (tokens: ${raw.input_tokens || 0}+${raw.output_tokens || 0})`;
        } else if (raw.__status__ === 'spawning') {
          div.textContent = `\u26a1 Launching ${raw.count} subagent(s)...`;
        } else if (raw.__status__ === 'spawn_complete') {
          div.className = 'chat-cost-line';
          div.textContent = `\u2713 All ${raw.count} subagent(s) complete (tokens: ${raw.input_tokens || 0}+${raw.output_tokens || 0})`;
        } else if (raw.__status__ === 'compacting') {
          div.textContent = '\u27f3 Compacting conversation history...';
        } else {
          div.textContent = raw.__status__;
        }
        streamEl.appendChild(div);
        textDiv = null;
        currentBlockText = '';
      }
      return;
    }

    // --- Plain text lines (from CLI provider or streamed text) ---
    if (typeof raw !== 'string') return;
    const line = raw;
    const cur = _chatSessions[todoId];

    const streamEl = document.getElementById('chat-assistant-streaming');

    if (streamEl) {
      // Legacy string-based tool/status lines (from CLI provider)
      if (line.startsWith('\u25b6 ')) {
        const div = document.createElement('div');
        div.className = 'chat-tool-line';
        div.textContent = line;
        streamEl.appendChild(div);
        textDiv = null;
        currentBlockText = '';
      } else {
        cur.streamingText += (cur.streamingText ? '\n' : '') + line;
        currentBlockText += (currentBlockText ? '\n' : '') + line;
        if (!textDiv || !textDiv.parentNode) {
          textDiv = document.createElement('div');
          textDiv.className = 'chat-assistant-block';
          streamEl.appendChild(textDiv);
        }
        textDiv.innerHTML = renderMd(currentBlockText);
      }
    } else {
      // Panel is minimized — just accumulate text (strings only)
      if (typeof raw === 'string') {
        cur.streamingText += (cur.streamingText ? '\n' : '') + line;
      }
    }

    const log = document.getElementById('chat-log');
    if (log) log.scrollTop = log.scrollHeight;
  };

  es.onerror = function() {
    es.close();
    if (isStale()) return;
    const cur = _chatSessions[todoId];
    _chatStreamDone(todoId, null, cur ? cur.streamingText : '');
  };
}

function _chatStreamDone(todoId, error, assistantText) {
  const session = _chatSessions[todoId];
  if (session) {
    session.streamingJobId = null;
    session.streamingText = '';
  }
  _updateSpinnersInPlace();

  if (_activeChatTodoId === todoId) {
    _syncChatSendBtn(todoId);
    const spinner = document.getElementById('chat-spinner');
    if (spinner) spinner.remove();
  }

  if (error) {
    if (_activeChatTodoId === todoId) {
      const log = document.getElementById('chat-log');
      if (log) log.insertAdjacentHTML('beforeend', '<div style="color:#ef4444">' + esc(error) + '</div>');
    }
  
    return;
  }

  if (session && assistantText) {
    session.messages.push({ role: 'assistant', content: assistantText });
  }

  // If chat panel is open for this item, re-render the log
  if (_activeChatTodoId === todoId) {
    _renderChatLog(todoId);
  }

  const input = document.getElementById('chat-input');
  if (input) input.focus();
}

async function stopChat(todoId) {
  const session = _chatSessions[todoId];
  if (!session || !session.streamingJobId || session.streamingJobId === 'pending') return;
  try {
    await fetch('/api/jobs/' + session.streamingJobId + '/kill', { method: 'POST', headers: {'Content-Type': 'application/json'} });
  } catch {}
}

async function _loadChatSession(todoId) {
  try {
    const res = await fetch('/api/chats/' + todoId);
    if (!res.ok) return;
    const data = await res.json();
    const existing = _chatSessions[todoId];
    if (existing) {
      existing.conversationId = data.conversationId || existing.conversationId;
      existing.messages = data.messages || existing.messages;
    } else {
      _chatSessions[todoId] = {
        conversationId: data.conversationId || null,
        messages: data.messages || [],
        streamingText: '',
        streamingJobId: null,
      };
    }
    // If server reports a running job, reconnect the SSE stream
    if (data.running_job_id) {
      const session = _chatSessions[todoId];
      if (!session.streamingJobId) {
        session.streamingJobId = data.running_job_id;
        session.streamingText = '';
        _streamChatResponse(todoId, data.running_job_id);
      }
    }
  } catch {}
}

function _startChatResize(e) {
  e.preventDefault();
  const panel = document.getElementById('chat-panel');
  const isTouch = e.type === 'touchstart';
  const startY = isTouch ? e.touches[0].clientY : e.clientY;
  const startH = panel.offsetHeight;
  function onMove(e) {
    const y = e.touches ? e.touches[0].clientY : e.clientY;
    const h = Math.min(window.innerHeight * 0.9, Math.max(150, startH - (y - startY)));
    panel.style.height = h + 'px';
  }
  function onUp() {
    document.removeEventListener('mousemove', onMove);
    document.removeEventListener('mouseup', onUp);
    document.removeEventListener('touchmove', onMove);
    document.removeEventListener('touchend', onUp);
  }
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);
  document.addEventListener('touchmove', onMove, { passive: false });
  document.addEventListener('touchend', onUp);
}

async function eaUpdateToggle() {
  const isRunning = Object.values(_jobsState).some(
    j => j.job_key === 'ea-update' && (j.status === 'running' || j.status === 'pending')
  );
  if (isRunning) await cancelEaUpdate(); else await eaUpdate();
}

async function eaUpdate(force) {
  try {
    const res = await fetch('/api/ea-update', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({force: !!force}) });
    const data = await res.json();
    if (res.ok && data.status === 'already_running') {
      showToast('Already running — click to restart', false, () => eaUpdate(true));
    } else if (res.ok) {
      showToast('EA update started', false);
      if (data.job_id) {
        // Animate immediately — don't wait for pollJobs round trip
        if (!_eaWasRunning) { _eaWasRunning = true; _transitionEaBtn(true); }
        const btn = document.getElementById('ea-update-btn');
        if (btn) btn.classList.add('running');
        _openEaUpdateStream(data.job_id);
        pollJobs();
      }
    } else {
      showToast(data.error || 'Failed to start EA update', true);
    }
  } catch (e) {
    showToast('Failed to start EA update', true);
  }
}

async function toggleTodoHistory(todoId) {
  if (!todoId) todoId = selectedTodoId;
  if (!todoId) { showToast('Select a todo first'); return; }
  const overlay = document.getElementById('history-overlay');
  if (overlay.style.display === 'flex' && overlay.dataset.todoId === todoId) {
    closeHistoryPanel();
    return;
  }
  overlay.dataset.todoId = todoId;
  const todo = allTodos.find(t => t.id === todoId);
  document.getElementById('history-title').textContent = todo ? (todo.title || todoId).slice(0, 60) : todoId;
  const log = document.getElementById('history-log');
  log.innerHTML = '<div style="padding:20px;color:var(--subtle)">Loading...</div>';
  overlay.style.display = 'flex';
  requestAnimationFrame(() => overlay.classList.add('visible'));
  await _loadTodoHistory(todoId);
}

function closeHistoryPanel() {
  const overlay = document.getElementById('history-overlay');
  overlay.classList.remove('visible');
  setTimeout(() => { overlay.style.display = 'none'; }, 100);
}

async function _loadTodoHistory(todoId) {
  const log = document.getElementById('history-log');
  if (!log) return;
  try {
    const res = await fetch('/api/todos/' + todoId + '/history');
    const data = await res.json();
    const entries = data.entries || [];
    if (entries.length === 0) {
      log.innerHTML = '<div style="padding:20px;color:var(--subtle)">No history for this item.</div>';
      return;
    }
    let html = '';
    entries.forEach((e, i) => {
      const date = new Date(e.changed_at).toLocaleString(undefined, {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
      const snap = e.snapshot || {};
      const title = snap.title || '(untitled)';
      const desc = (snap.description || '').replace(/\n/g, '\n');
      const isCurrent = i === 0;
      html += '<div style="padding:10px 14px;border-bottom:1px solid rgba(255,255,255,0.06)' + (isCurrent ? ';background:rgba(79,110,247,0.08)' : '') + '">'
        + '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
        + '<div style="flex:1"><span style="font-size:0.8rem;color:#e2e8f0;font-weight:500">' + esc(title) + '</span>'
        + ' <span style="font-size:0.65rem;color:var(--subtle)">' + esc(e.action) + ' — ' + date + '</span></div>'
        + '<button onclick="restoreTodoVersion(' + e.id + ',\'' + esc(todoId) + '\')" style="border:none;background:var(--accent);color:#fff;padding:3px 10px;border-radius:5px;cursor:pointer;font-size:0.7rem">Restore</button>'
        + '</div>';
      if (desc) {
        html += '<pre style="font-size:0.72rem;color:var(--muted);white-space:pre-wrap;word-break:break-word;margin:0;max-height:150px;overflow-y:auto">' + esc(desc) + '</pre>';
      }
      html += '</div>';
    });
    log.innerHTML = html;
  } catch {
    log.innerHTML = '<div style="padding:20px;color:var(--danger)">Failed to load history.</div>';
  }
}

function _toggleCheckonBody(headerEl) {
  const sumEl = headerEl.parentElement;
  if (!sumEl) return;
  const collapsed = sumEl.classList.toggle('collapsed');
  headerEl.classList.toggle('collapsed', collapsed);
}

async function consolidateItem(todoId) {
  try {
    const res = await fetch('/api/ea-update-item', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id: todoId, message: '/ea consolidate description ' + todoId})
    });
    const data = await res.json();
    if (res.ok && data.job_id) {
      showToast('Consolidating...');
      _jobsState[data.job_id] = { id: data.job_id, job_key: 'ea-' + todoId, status: 'running', created_at: Date.now()/1000, _optimistic: true };
      _updateSpinnersInPlace();
      _openItemStream(todoId, data.job_id);
    }
  } catch { showToast('Failed', true); }
}

async function restoreTodoVersion(historyId, todoId) {
  if (!confirm('Restore this version?')) return;
  try {
    const res = await fetch('/api/history/' + historyId + '/restore', { method: 'POST', headers: {'Content-Type': 'application/json'} });
    if (res.ok) {
      showToast('Restored');
      loadTodos();
    } else {
      const data = await res.json();
      showToast(data.error || 'Failed to restore', true);
    }
  } catch { showToast('Failed to restore', true); }
}

async function eaUpdateItem(id, force) {
  try {
    const res = await fetch('/api/ea-update-item', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({id, force: !!force})
    });
    const data = await res.json();
    if (res.ok && data.status === 'already_running') {
      if (data.job_id) await killJob(data.job_id);
      return;
    } else if (res.ok) {
      showToast(`Checking ${id}...`, false);
      if (data.job_id) {
        // Show spinner immediately — don't wait for pollJobs round trip
        _jobsState[data.job_id] = { id: data.job_id, job_key: 'ea-' + id, status: 'running', created_at: Date.now()/1000, _optimistic: true };
        _updateSpinnersInPlace();
        _openItemStream(id, data.job_id);
      }
    } else {
      showToast(data.error || 'Failed', true);
    }
  } catch (e) {
    // silent
  }
}

// ---- Shared toast helper ----
function showToast(msg, isError, onClick) {
  const toast = document.createElement('div');
  toast.textContent = msg;
  toast.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:' + (isError ? 'var(--danger)' : 'var(--text)') + ';color:var(--card);padding:8px 16px;border-radius:8px;font-size:0.85rem;z-index:5000;box-shadow:var(--shadow-lg);opacity:0;transition:opacity .15s' + (onClick ? ';cursor:pointer' : '');
  if (onClick) toast.addEventListener('click', () => { toast.remove(); onClick(); });
  document.body.appendChild(toast);
  requestAnimationFrame(() => { toast.style.opacity = '1'; });
  setTimeout(() => { toast.style.opacity = '0'; setTimeout(() => toast.remove(), 150); }, 2000);
}

// ---- Inline job streaming ----
let _jobsPollTimer = null;
let _jobsState = {};          // jobId -> job metadata (from server)
let _clientJobLines = {};     // todoId -> string[] of parsed display lines
let _clientJobSummary = {};   // todoId -> string[] of message-only lines (no tool calls)
let _clientJobIds = {};       // todoId -> jobId that populated _clientJobLines
let _clientJobStartTime = {}; // todoId -> Date when checkon started
let _itemStreamSources = {};  // todoId -> EventSource
let _eaUpdateTimerInterval = null;
let _eaWasRunning = false;
let _eaUpdateBubbleLines = [];
let _eaUpdateBubbleStream = null; // EventSource for ea-update output

// Interactive terminal sessions
let _termSessions = {};       // todoId -> {sessionId, term, fitAddon, ws, alive}
let _activeTermTodoId = null;  // which session is visible in the overlay
let _termResizeObserver = null;

// Chat sessions
let _chatSessions = {};       // todoId -> { conversationId, messages: [{role, content}] }
let _activeChatTodoId = null;  // which chat is visible in the overlay
let _chatUnread = new Set();   // todoIds with unread chat responses
let _eaTransitionTimer = null;

function _todoIdForJob(job) {
  if (!job || !job.job_key) return null;
  const key = job.job_key;
  if (key.startsWith('workon-')) return key.slice(7);
  if (key.startsWith('chat-')) return key.slice(5);
  if (key.startsWith('ea-') && key !== 'ea-update') return key.slice(3);
  return null;
}

function _getActiveJobForTodo(todoId) {
  let fallback = null;
  for (const j of Object.values(_jobsState)) {
    if (_todoIdForJob(j) !== todoId || j.status === 'killed') continue;
    if (j.status === 'running' || j.status === 'pending') return j;
    if (!fallback) fallback = j;
  }
  return fallback;
}

function _openItemStream(todoId, jobId) {
  if (_itemStreamSources[todoId]) {
    _itemStreamSources[todoId].close();
    delete _itemStreamSources[todoId];
  }
  // New job for this todo — start fresh output
  if (_clientJobIds[todoId] !== jobId) {
    _clientJobLines[todoId] = [];
    _clientJobSummary[todoId] = [];
    _clientJobIds[todoId] = jobId;
    _clientJobStartTime[todoId] = new Date();
    // Clear summary div and add spinner
    const sumEl = document.getElementById('checkon-summary-' + todoId);
    if (sumEl) {
      const timeStr = _clientJobStartTime[todoId].toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
      sumEl.innerHTML = '<div class="checkon-header" onclick="event.stopPropagation();_toggleCheckonBody(this)"><span>Status check — ' + timeStr + '</span><span class="checkon-arrow">&#9660;</span></div><div class="checkon-body"></div><l-bouncy class="checkon-inline-spinner" size="20" speed="1.75" color="var(--muted)"></l-bouncy>';
      sumEl.classList.add('has-content');
    }
  }
  if (!_clientJobLines[todoId]) _clientJobLines[todoId] = [];
  if (!_clientJobSummary[todoId]) _clientJobSummary[todoId] = [];
  const existingCount = _clientJobLines[todoId].length;
  let parsedCount = 0;
  const src = new EventSource('/api/jobs/' + jobId + '/stream');
  _itemStreamSources[todoId] = src;
  src.onmessage = (e) => {
    let raw;
    try { raw = JSON.parse(e.data); } catch { return; }
    if (typeof raw === 'object' && raw.__done__) {
      src.close(); delete _itemStreamSources[todoId];
      // Mark bubble as done so hover no longer shows it
      const bubble = document.getElementById('checkon-bubble-' + todoId);
      if (bubble) bubble.classList.add('done');
      // Remove spinner and add completion footer
      const sumDone = document.getElementById('checkon-summary-' + todoId);
      if (sumDone) {
        const spinner = sumDone.querySelector('.checkon-inline-spinner');
        if (spinner) spinner.remove();
        const now = new Date();
        const timeStr = now.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
        const footer = document.createElement('div');
        footer.className = 'checkon-footer';
        footer.textContent = 'Complete — ' + timeStr;
        sumDone.appendChild(footer);
      }
      pollJobs();
      return;
    }
    // --- Structured event handling ---
    if (typeof raw === 'object') {
      if (raw.__tool_call__ || raw.__status__ || raw.__error__) {
        parsedCount++;
        let text = '';
        let isStatus = false;
        if (raw.__tool_call__) {
          text = raw.subagent ? `[${raw.subagent}] \u25b6 ${raw.name}...` : `\u25b6 ${raw.name}...`;
        } else if (raw.__error__) {
          text = raw.message;
        } else if (raw.__status__ === 'done') {
          text = raw.cost != null ? `\u2713 Done \u2014 $${raw.cost.toFixed(4)}` : `\u2713 Done (tokens: ${raw.input_tokens || 0}+${raw.output_tokens || 0})`;
          isStatus = true;
        } else if (raw.__status__ === 'spawning') {
          text = `\u26a1 Launching ${raw.count} subagent(s)...`;
          isStatus = true;
        } else if (raw.__status__ === 'spawn_complete') {
          text = `\u2713 All ${raw.count} subagent(s) complete (tokens: ${raw.input_tokens || 0}+${raw.output_tokens || 0})`;
          isStatus = true;
        } else if (raw.__status__ === 'compacting') {
          text = '\u27f3 Compacting conversation history...';
          isStatus = true;
        }
        const outEl = document.getElementById('checkon-bubble-' + todoId);
        if (outEl && text) {
          outEl.classList.add('has-content');
          const div = document.createElement('div');
          if (isStatus) div.style.color = 'var(--accent)';
          div.textContent = text;
          outEl.appendChild(div);
          outEl.scrollTop = outEl.scrollHeight;
        }
        return;
      }
      return; // unknown structured object
    }
    // --- Plain text lines ---
    const line = parseStreamLine(raw);
    if (!line) return;
    if (parsedCount < existingCount) { parsedCount++; return; }
    console.log(`[job:${jobId}]`, line);
    _clientJobLines[todoId].push(line);
    parsedCount++;
    // Bubble: all lines
    const outEl = document.getElementById('checkon-bubble-' + todoId);
    if (outEl) {
      outEl.classList.add('has-content');
      const div = document.createElement('div');
      div.textContent = line;
      outEl.appendChild(div);
      outEl.scrollTop = outEl.scrollHeight;
    }
    // Summary: only text lines (structured events already filtered above)
    _clientJobSummary[todoId].push('\u23fa ' + line);
    const sumEl = document.getElementById('checkon-summary-' + todoId);
    if (sumEl) {
      sumEl.classList.add('has-content');
      const bodyEl = sumEl.querySelector('.checkon-body');
      if (bodyEl) bodyEl.textContent = _clientJobSummary[todoId].join('\n');
    }
  };
  src.onerror = () => { src.close(); delete _itemStreamSources[todoId]; };
}

function _updateSpinnersInPlace() {
  document.querySelectorAll('.todo-item[data-todo-id]').forEach(el => {
    const todoId = el.dataset.todoId;
    const job = _getActiveJobForTodo(todoId);
    const termSession = _termSessions[todoId];
    const chatSession = _chatSessions[todoId];
    const titleEl = el.querySelector('.todo-title');
    if (!titleEl) return;
    const hasTerminal = termSession && termSession.alive;
    const hasChatStreaming = !!(chatSession && chatSession.streamingJobId);
    const hasJob = job && job.status === 'running' && !(job.job_key && job.job_key.startsWith('chat-'));

    // Terminal spinner (green, opens terminal on click)
    const existingTermSpinner = titleEl.querySelector('.term-spinner');
    if (hasTerminal) {
      if (!existingTermSpinner) {
        const s = document.createElement('span');
        s.className = 'job-spinner term-spinner'; s.title = 'Open terminal';
        s.innerHTML = '<span class="sk-child"></span><span class="sk-child sk-bounce2"></span>';
        s.onclick = e => { e.stopPropagation(); openTerminal(todoId); };
        titleEl.insertBefore(s, titleEl.firstChild);
      }
    } else {
      if (existingTermSpinner) existingTermSpinner.remove();
    }

    // Chat spinner (green jelly-triangle, opens chat on click)
    const existingChatSpinner = titleEl.querySelector('.chat-spinner');
    if (hasChatStreaming) {
      if (!existingChatSpinner) {
        const s = document.createElement('span');
        s.className = 'job-spinner chat-spinner'; s.title = 'Open chat';
        s.innerHTML = '<l-jelly-triangle size="13" speed="1.75" color="#22c55e"></l-jelly-triangle>';
        s.onclick = e => { e.stopPropagation(); openChat(todoId); };
        const after = titleEl.querySelector('.term-spinner');
        titleEl.insertBefore(s, after ? after.nextSibling : titleEl.firstChild);
      }
    } else {
      if (existingChatSpinner) existingChatSpinner.remove();
    }

    // Job spinner (accent color, kills job on click) — non-chat jobs only
    const existingJobSpinner = titleEl.querySelector('.job-spinner:not(.term-spinner):not(.chat-spinner)');
    if (hasJob) {
      if (!existingJobSpinner) {
        const s = document.createElement('span');
        s.className = 'job-spinner'; s.title = 'Stop job';
        s.innerHTML = '<l-jelly-triangle size="13" speed="1.75" color="var(--accent)"></l-jelly-triangle>';
        s.onclick = e => { e.stopPropagation(); killJob(job.id); };
        const insertBefore = titleEl.querySelector('.term-spinner') ? titleEl.querySelector('.term-spinner').nextSibling : titleEl.firstChild;
        titleEl.insertBefore(s, insertBefore);
      } else {
        existingJobSpinner.onclick = e => { e.stopPropagation(); killJob(job.id); };
      }
    } else {
      if (existingJobSpinner) existingJobSpinner.remove();
    }

    // Unread dot — from agent tool writes only
    const hasUnread = _chatUnread.has(todoId);
    const existingUnreadDot = titleEl.querySelector('.chat-unread-dot');
    if (hasUnread) {
      if (!existingUnreadDot) {
        const dot = document.createElement('span');
        dot.className = 'chat-unread-dot';
        dot.innerHTML = '<l-ripples size="13" speed="2" color="#f59e0b"></l-ripples>';
        dot.onclick = e => { e.stopPropagation(); openChat(todoId); };
        dot.style.cursor = 'pointer';
        titleEl.insertBefore(dot, titleEl.firstChild);
      }
    } else {
      if (existingUnreadDot) existingUnreadDot.remove();
    }
  });
}

function _restoreJobOutputs() {
  for (const [todoId, lines] of Object.entries(_clientJobLines)) {
    if (!lines.length) continue;
    const outEl = document.getElementById('checkon-bubble-' + todoId);
    if (outEl) {
      // If job is done (no active stream), mark bubble as done so hover won't show it
      const isDone = !_itemStreamSources[todoId];
      outEl.classList.add('has-content');
      if (isDone) outEl.classList.add('done');
      outEl.innerHTML = lines.map(l => {
        const style = l.startsWith('✓') ? ' style="color:var(--accent)"' : '';
        const d = document.createElement('div'); d.textContent = l;
        return `<div${style}>${d.innerHTML}</div>`;
      }).join('');
      outEl.scrollTop = outEl.scrollHeight;
    }
  }
  // Restore summaries
  for (const [todoId, lines] of Object.entries(_clientJobSummary)) {
    if (!lines.length) continue;
    const sumEl = document.getElementById('checkon-summary-' + todoId);
    if (sumEl) {
      sumEl.innerHTML = '';
      sumEl.classList.add('has-content');
      const startTime = _clientJobStartTime[todoId];
      const isStreaming = !!_itemStreamSources[todoId];
      if (startTime) {
        const timeStr = startTime.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
        const header = document.createElement('div');
        header.className = 'checkon-header';
        header.onclick = (e) => { e.stopPropagation(); _toggleCheckonBody(header); };
        header.innerHTML = '<span>Status check \u2014 ' + timeStr + '</span><span class="checkon-arrow">&#9660;</span>';
        sumEl.appendChild(header);
      }
      const body = document.createElement('div');
      body.className = 'checkon-body';
      body.textContent = lines.join('\n');
      sumEl.appendChild(body);
      if (isStreaming) {
        const spinner = document.createElement('l-bouncy');
        spinner.className = 'checkon-inline-spinner';
        spinner.setAttribute('size', '20');
        spinner.setAttribute('speed', '1.75');
        spinner.setAttribute('color', 'var(--muted)');
        sumEl.appendChild(spinner);
      }
    }
  }
}

function parseStreamLine(raw) {
  if (typeof raw !== 'string') return null;
  return raw.trim() || null;
}

async function killJob(jobId) {
  await fetch('/api/jobs/' + jobId + '/kill', { method: 'POST', headers: {'Content-Type': 'application/json'} });
  pollJobs();
}

function _transitionEaBtn(toRunning) {
  if (_eaTransitionTimer) { clearTimeout(_eaTransitionTimer); _eaTransitionTimer = null; }
  const lbl = document.getElementById('ea-update-label');
  const run = document.getElementById('ea-update-running');
  if (!lbl || !run) return;

  lbl.classList.remove('anim-in', 'anim-out');
  run.classList.remove('anim-in', 'anim-out');
  void lbl.offsetWidth; // force reflow

  const outEl = toRunning ? lbl : run;
  const inEl  = toRunning ? run : lbl;

  // Ensure outgoing starts at opacity:1, incoming at opacity:0
  outEl.style.opacity = '1';
  inEl.style.opacity = '0';
  void inEl.offsetWidth;

  outEl.classList.add('anim-out');
  inEl.classList.add('anim-in');

  _eaTransitionTimer = setTimeout(() => {
    outEl.classList.remove('anim-out'); outEl.style.opacity = '0';
    inEl.classList.remove('anim-in');   inEl.style.opacity = '1';
    _eaTransitionTimer = null;
  }, 400);
}

function _updateEaUpdateBtn() {
  const btn = document.getElementById('ea-update-btn');
  if (!btn) return;

  const job = Object.values(_jobsState).find(
    j => j.job_key === 'ea-update' && (j.status === 'running' || j.status === 'pending')
  );
  const isRunning = !!job;

  if (isRunning !== _eaWasRunning) {
    _eaWasRunning = isRunning;
    _transitionEaBtn(isRunning);
  }

  if (isRunning) {
    btn.classList.add('running');
    if (_eaUpdateTimerInterval) clearInterval(_eaUpdateTimerInterval);
    const tick = () => {
      const timer = document.getElementById('ea-update-timer');
      if (!timer) return;
      const elapsed = Math.floor(Date.now() / 1000 - job.created_at);
      const m = Math.floor(elapsed / 60);
      const s = String(elapsed % 60).padStart(2, '0');
      timer.textContent = m > 0 ? `${m}:${s}` : `0:${s}`;
    };
    tick();
    _eaUpdateTimerInterval = setInterval(tick, 1000);
  } else {
    btn.classList.remove('running');
    if (_eaUpdateTimerInterval) { clearInterval(_eaUpdateTimerInterval); _eaUpdateTimerInterval = null; }
  }
}

async function cancelEaUpdate() {
  const job = Object.values(_jobsState).find(
    j => j.job_key === 'ea-update' && (j.status === 'running' || j.status === 'pending')
  );
  if (job) await killJob(job.id);
}

function _restoreEaUpdateBubble() {
  const bubble = document.getElementById('ea-update-bubble');
  if (!bubble || !_eaUpdateBubbleLines.length) return;
  bubble.innerHTML = '';
  bubble.classList.add('has-content');
  for (const line of _eaUpdateBubbleLines) {
    const div = document.createElement('div');
    if (line.startsWith('✓')) div.style.color = 'var(--accent)';
    div.textContent = line;
    bubble.appendChild(div);
  }
  bubble.scrollTop = bubble.scrollHeight;
}

function _appendEaUpdateBubbleLine(line) {
  _eaUpdateBubbleLines.push(line);
  const bubble = document.getElementById('ea-update-bubble');
  if (!bubble) return;
  bubble.classList.add('has-content');
  const div = document.createElement('div');
  if (line.startsWith('✓')) div.style.color = 'var(--accent)';
  div.textContent = line;
  bubble.appendChild(div);
  bubble.scrollTop = bubble.scrollHeight;
}

function _openEaUpdateStream(jobId) {
  if (_eaUpdateBubbleStream) { _eaUpdateBubbleStream.close(); _eaUpdateBubbleStream = null; }
  _eaUpdateBubbleLines = [];
  const bubble = document.getElementById('ea-update-bubble');
  if (bubble) { bubble.textContent = ''; bubble.classList.remove('has-content'); }

  const src = new EventSource('/api/jobs/' + jobId + '/stream');
  _eaUpdateBubbleStream = src;
  src.onmessage = (e) => {
    let raw;
    try { raw = JSON.parse(e.data); } catch { return; }
    if (typeof raw === 'object' && raw.__done__) {
      src.close(); _eaUpdateBubbleStream = null;
      return;
    }
    const line = parseStreamLine(raw);
    if (!line) return;
    console.log('[ea-update]', line);
    _appendEaUpdateBubbleLine(line);
  };
  src.onerror = () => { src.close(); _eaUpdateBubbleStream = null; };
}

async function pollJobs() {
  clearTimeout(_jobsPollTimer);
  try {
    const res = await fetch('/api/jobs');
    const jobs = await res.json();
    const serverIds = new Set(jobs.map(j => j.id));
    // Remove entries not on server (completed/purged), keep optimistic entries for jobs server hasn't seen yet
    for (const id of Object.keys(_jobsState)) {
      if (!serverIds.has(id) && _jobsState[id]._optimistic) {
        // Keep optimistic entry until server confirms
      } else if (!serverIds.has(id)) {
        delete _jobsState[id];
      }
    }
    // Update/add from server (server is authoritative)
    jobs.forEach(j => { _jobsState[j.id] = j; });
    // Open streams for running jobs that don't have one yet
    for (const j of jobs) {
      if (j.status !== 'running') continue;
      if (j.job_key === 'ea-update' && !_eaUpdateBubbleStream) {
        _openEaUpdateStream(j.id);
      }
      const todoId = _todoIdForJob(j);
      if (todoId && !_itemStreamSources[todoId] && !j.job_key.startsWith('chat-')) _openItemStream(todoId, j.id);
    }
    // Clear client lines for jobs that are gone
    for (const todoId of Object.keys(_clientJobLines)) {
      const stillActive = jobs.some(j => _todoIdForJob(j) === todoId && j.status !== 'killed');
      if (!stillActive) delete _clientJobLines[todoId];
    }
    _updateSpinnersInPlace();
    _restoreJobOutputs();
    _updateEaUpdateBtn();
    // Poll terminal sessions too
    try {
      const tRes = await fetch('/api/terminal/sessions');
      const sessions = await tRes.json();
      const aliveIds = new Set();
      const serverSessions = {};
      for (const s of sessions) {
        if (s.alive) { aliveIds.add(s.todo_id); serverSessions[s.todo_id] = s; }
      }
      // Create placeholder entries for server-known sessions missing on client
      for (const [todoId, s] of Object.entries(serverSessions)) {
        if (!_termSessions[todoId]) {
          _termSessions[todoId] = { sessionId: s.session_id, term: null, fitAddon: null, ws: null, alive: true };
        }
      }
      // Mark dead sessions on client
      for (const [todoId, ts] of Object.entries(_termSessions)) {
        if (!aliveIds.has(todoId) && ts.alive) {
          ts.alive = false;
          if (ts.term) ts.term.writeln('\\r\\n\\x1b[2m[session ended]\\x1b[0m');
        }
      }
      _updateSpinnersInPlace();
    } catch {}
    // Poll chat unread state
    try {
      // Unread state is pushed via SSE streams — no polling needed
    } catch {}
  } catch {}
}

function fallbackCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  document.execCommand('copy');
  document.body.removeChild(ta);
}

async function bringToTop(id) {
  const res = await fetch(API + '/move-to-top', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id})
  });
  if (!res.ok) return;
  await loadTodos();
  const ni = visibleIds.indexOf(id);
  if (ni >= 0) selectedIdx = ni + 1;
  applySelection();
}

async function moveSelected(direction) {
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return;
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo || todo.status === 'completed') return;
  const res = await fetch(API + '/reorder', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id, direction})
  });
  if (!res.ok) return;
  const rememberedId = id;
  await loadTodos();
  const newIdx = visibleIds.indexOf(rememberedId);
  if (newIdx >= 0) selectedIdx = newIdx + 1;
  applySelection();
}

async function moveSectionSelected(direction) {
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return;
  const curId = visibleIds[selectedIdx - 1];
  if (!curId.startsWith('__section__:')) return;
  const section = curId.slice('__section__:'.length);
  if (section === '__completed__') return;

  const idx = sectionsOrder.indexOf(section);
  if (idx < 0) return;

  let beforeSection = null;
  if (direction === 'up') {
    // Move before the previous section; skip empty-string (unsectioned)
    let target = idx - 1;
    while (target >= 0 && sectionsOrder[target] === '') target--;
    if (target < 0) return;
    beforeSection = sectionsOrder[target];
  } else {
    // Move after the next section = move before the one two ahead
    let target = idx + 1;
    if (target >= sectionsOrder.length) return;
    // Place before the section that's two positions ahead, or null (end)
    beforeSection = (target + 1 < sectionsOrder.length) ? sectionsOrder[target + 1] : null;
  }

  const res = await fetch('/api/sections/reorder', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({section, before_section: beforeSection})
  });
  if (!res.ok) return;
  await loadTodos();
  const markerIdx = visibleIds.indexOf('__section__:' + section);
  if (markerIdx >= 0) selectedIdx = markerIdx + 1;
  applySelection();
}

async function startEdit(id) {
  editingId = id;
  render();
  // Lazily load CodeMirror modules
  if (!cmModules) {
    const [cmBundle, cmMd, cmView, cmState, cmCmd] = await Promise.all([
      import('codemirror'),
      import('@codemirror/lang-markdown'),
      import('@codemirror/view'),
      import('@codemirror/state'),
      import('@codemirror/commands')
    ]);
    cmModules = {
      basicSetup: cmBundle.basicSetup, markdown: cmMd.markdown,
      markdownKeymap: cmMd.markdownKeymap,
      EditorView: cmView.EditorView, keymap: cmView.keymap,
      EditorState: cmState.EditorState,
      moveLineUp: cmCmd.moveLineUp, moveLineDown: cmCmd.moveLineDown,
    };
  }
  const { basicSetup, markdown, EditorView, keymap, EditorState, markdownKeymap, moveLineUp, moveLineDown } = cmModules;
  const t = allTodos.find(t => t.id === id);
  const descEl = document.getElementById('edit-desc-' + id);
  if (descEl) {
    if (cmEditor) { cmEditor.destroy(); cmEditor = null; }
    const customKeymap = keymap.of([
      { key: 'Mod-Enter', run: () => { saveEdit(id); return true; } },
      { key: 'Escape', run: () => { cancelEdit(); return true; } },
      { key: 'Alt-ArrowUp', run: moveLineUp },
      { key: 'Alt-ArrowDown', run: moveLineDown },
      { key: 'Mod-x', run: (view) => {
        const sel = view.state.selection.main;
        if (!sel.empty) return false; // default cut if text selected
        const line = view.state.doc.lineAt(sel.head);
        const text = view.state.sliceDoc(line.from, Math.min(line.to + 1, view.state.doc.length));
        navigator.clipboard.writeText(text);
        view.dispatch({ changes: { from: line.from, to: Math.min(line.to + 1, view.state.doc.length) } });
        return true;
      }},
    ]);
    cmEditor = new EditorView({
      doc: t ? t.description : '',
      extensions: [
        customKeymap,
        basicSetup,
        keymap.of(markdownKeymap),
        markdown(),
        EditorView.lineWrapping,
        EditorView.theme({
          '&': { maxHeight: '300px' },
          '.cm-scroller': { overflow: 'auto' }
        })
      ],
      parent: descEl
    });
    // Move cursor to end and focus
    cmEditor.dispatch({ selection: { anchor: cmEditor.state.doc.length } });
    cmEditor.focus();
  }
  setTimeout(() => {
    const editEls = document.querySelectorAll(`#edit-title-${id}, #edit-desc-${id}, #edit-priority-${id}, #edit-section-${id}, #edit-section-custom-${id}`);
    // Section dropdown: show/hide custom input
    const secSelect = document.getElementById('edit-section-' + id);
    const secCustom = document.getElementById('edit-section-custom-' + id);
    if (secSelect && secCustom) {
      secSelect.addEventListener('change', () => {
        if (secSelect.value === '__custom__') {
          secCustom.style.display = '';
          secCustom.focus();
        } else {
          secCustom.style.display = 'none';
          secCustom.value = '';
        }
      });
    }
    // Keydown: Cmd+Enter to save, Escape to cancel (for non-CM fields)
    editEls.forEach(el => {
      el.addEventListener('keydown', e => {
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); saveEdit(id); }
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancelEdit(); }
      });
    });
    // Cancel edit when clicking outside the todo item card
    const todoCard = descEl ? descEl.closest('.todo-item') : null;
    function onDocMousedown(e) {
      if (editingId !== id) { document.removeEventListener('mousedown', onDocMousedown, true); return; }
      if (todoCard && todoCard.contains(e.target)) return;
      e.preventDefault();
      cancelEdit();
    }
    document.addEventListener('mousedown', onDocMousedown, true);
  }, 0);
}

let _justCancelledEdit = false;
function cancelEdit() {
  const restoreId = editingId;
  if (cmEditor) { cmEditor.destroy(); cmEditor = null; }
  editingId = null;
  _justCancelledEdit = true;
  render();
  if (restoreId) selectTodo(restoreId);
}

async function saveEdit(id) {
  const editedTitle = document.getElementById('edit-title-' + id).value.trim();
  // Reconstruct full title preserving metadata tags from original
  const origTodo = allTodos.find(x => x.id === id);
  const origRaw = origTodo ? origTodo.title || '' : '';
  const metaMatch = origRaw.match(/`(?:updated|read)[^`]*`/);
  const meta = metaMatch ? ' ' + metaMatch[0] : '';
  const title = '**' + editedTitle + '**' + meta;
  const desc = cmEditor ? cmEditor.state.doc.toString().trim() : '';
  const priority = document.getElementById('edit-priority-' + id).value;
  const secSelect = document.getElementById('edit-section-' + id);
  const section = secSelect.value === '__custom__'
    ? document.getElementById('edit-section-custom-' + id).value.trim()
    : secSelect.value;
  if (!title) return;
  await fetch(API + '/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title, description: desc, priority, section})
  });
  if (cmEditor) { cmEditor.destroy(); cmEditor = null; }
  editingId = null;
  loadTodos();
}

// Enter key to add from title field
document.getElementById('new-title').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey && !e.metaKey && !e.ctrlKey) { e.preventDefault(); addTodo(); }
});

let _preAddSelectedIdx = -1;
let _preAddSelectedId = null;

function showAddForm() {
  addFormVisible = true;
  // Remember selection before opening form
  _preAddSelectedIdx = selectedIdx;
  _preAddSelectedId = (selectedIdx >= 1 && selectedIdx <= visibleIds.length) ? visibleIds[selectedIdx - 1] : null;
  const form = document.getElementById('add-form');
  form.classList.add('visible');
  // If a todo is selected, pre-fill section, set insertion point, and move form inline
  if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
    const selId = visibleIds[selectedIdx - 1];
    const selTodo = allTodos.find(t => t.id === selId);
    if (selTodo && selTodo.status !== 'completed') {
      document.getElementById('new-section').value = selTodo.section || '';
      insertBeforeId = selId;
      // Move form to appear right before the selected item
      const targetEl = document.querySelector(`.todo-item[data-todo-id="${selId}"]`);
      if (targetEl) targetEl.parentNode.insertBefore(form, targetEl);
    } else {
      insertBeforeId = null;
    }
  } else {
    insertBeforeId = null;
  }
  selectedIdx = SEL_ADD;
  applySelection();
  document.getElementById('new-title').focus();
}

function hideAddForm() {
  addFormVisible = false;
  insertBeforeId = null;
  const form = document.getElementById('add-form');
  form.classList.remove('visible');
  // Move form back to its default position (after the search bar)
  const searchBar = document.querySelector('.search-bar');
  if (searchBar) searchBar.after(form);
  document.getElementById('new-title').value = '';
  document.getElementById('new-desc').value = '';
  document.getElementById('new-priority').value = 'medium';
  document.getElementById('new-section').value = '';
  document.getElementById('new-section-custom').value = '';
  document.getElementById('new-section-custom').style.display = 'none';
  // Restore previous selection
  if (_preAddSelectedId) {
    const idx = visibleIds.indexOf(_preAddSelectedId);
    selectedIdx = idx >= 0 ? idx + 1 : (visibleIds.length > 0 ? 1 : -1);
  } else {
    selectedIdx = visibleIds.length > 0 ? 1 : -1;
  }
  _preAddSelectedId = null;
  _preAddSelectedIdx = -1;
  applySelection();
}

// selectedIdx: -1=nothing, 0=add-form, 1..N=todo items (1-indexed into visibleIds)
// ---------------------------------------------------------------------------
// Drag and drop
// ---------------------------------------------------------------------------
let dragId = null;
let dragSectionName = null; // non-null when dragging a section header

function clearAllDragIndicators() {
  document.querySelectorAll('.drag-over-top,.drag-over-bottom').forEach(el => {
    el.classList.remove('drag-over-top', 'drag-over-bottom');
  });
  document.querySelectorAll('.drag-over-section,.section-drag-over-top,.section-drag-over-bottom').forEach(el => {
    el.classList.remove('drag-over-section', 'section-drag-over-top', 'section-drag-over-bottom');
  });
}

document.addEventListener('dragstart', e => {
  // Section header drag
  const sectionRow = e.target.closest('.section-header-row[draggable]');
  if (sectionRow && !e.target.closest('.todo-item')) {
    dragSectionName = sectionRow.dataset.section;
    dragId = null;
    sectionRow.classList.add('section-dragging');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', 'section:' + dragSectionName);
    return;
  }
  // Todo item drag — only from the title handle
  const titleEl = e.target.closest('.todo-title[draggable]');
  if (!titleEl) return;
  const item = titleEl.closest('.todo-item');
  if (!item) return;
  dragId = item.dataset.todoId;
  dragSectionName = null;
  item.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', dragId);
});

document.addEventListener('dragend', e => {
  dragId = null;
  dragSectionName = null;
  document.querySelectorAll('.dragging').forEach(el => el.classList.remove('dragging'));
  document.querySelectorAll('.section-dragging').forEach(el => el.classList.remove('section-dragging'));
  clearAllDragIndicators();
});

document.addEventListener('dragover', e => {
  // --- Section header being dragged ---
  if (dragSectionName !== null) {
    const targetHeader = e.target.closest('.section-header-row[data-section]');
    if (targetHeader && targetHeader.dataset.section !== dragSectionName) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      clearAllDragIndicators();
      const rect = targetHeader.getBoundingClientRect();
      const midY = rect.top + rect.height / 2;
      if (e.clientY < midY) {
        targetHeader.classList.add('section-drag-over-top');
      } else {
        targetHeader.classList.add('section-drag-over-bottom');
      }
    }
    return;
  }

  // --- Todo item being dragged ---
  if (!dragId) return;
  const item = e.target.closest('.todo-item[data-todo-id]');
  const sectionHeader = e.target.closest('.section-header-row');

  if (item && item.dataset.todoId !== dragId) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    clearAllDragIndicators();
    const rect = item.getBoundingClientRect();
    const midY = rect.top + rect.height / 2;
    if (e.clientY < midY) {
      item.classList.add('drag-over-top');
    } else {
      item.classList.add('drag-over-bottom');
    }
  } else if (sectionHeader) {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    clearAllDragIndicators();
    sectionHeader.classList.add('drag-over-section');
  }
});

document.addEventListener('dragleave', e => {
  const item = e.target.closest('.todo-item');
  if (item) item.classList.remove('drag-over-top', 'drag-over-bottom');
  const sectionHeader = e.target.closest('.section-header-row');
  if (sectionHeader) sectionHeader.classList.remove('drag-over-section', 'section-drag-over-top', 'section-drag-over-bottom');
});

document.addEventListener('drop', async e => {
  // --- Section header drop ---
  if (dragSectionName !== null) {
    e.preventDefault();
    clearAllDragIndicators();
    const targetHeader = e.target.closest('.section-header-row[data-section]');
    if (!targetHeader || targetHeader.dataset.section === dragSectionName) {
      dragSectionName = null;
      return;
    }
    const targetSection = targetHeader.dataset.section;
    const rect = targetHeader.getBoundingClientRect();
    const midY = rect.top + rect.height / 2;

    // Determine where to place the dragged section
    let beforeSection;
    if (e.clientY < midY) {
      // Drop above target
      beforeSection = targetSection;
    } else {
      // Drop below target — find the section after targetSection
      const targetIdx = sectionsOrder.indexOf(targetSection);
      beforeSection = (targetIdx + 1 < sectionsOrder.length) ? sectionsOrder[targetIdx + 1] : null;
    }
    // Don't move if it would end up in the same spot
    const curIdx = sectionsOrder.indexOf(dragSectionName);
    const beforeIdx = beforeSection !== null ? sectionsOrder.indexOf(beforeSection) : sectionsOrder.length;
    if (curIdx === beforeIdx || curIdx + 1 === beforeIdx) {
      dragSectionName = null;
      return;
    }

    const payload = {section: dragSectionName};
    if (beforeSection !== null) payload.before_section = beforeSection;
    await fetch('/api/sections/reorder', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    dragSectionName = null;
    await loadTodos();
    return;
  }

  // --- Todo item drop ---
  if (!dragId) return;
  e.preventDefault();
  const item = e.target.closest('.todo-item[data-todo-id]');
  const sectionHeader = e.target.closest('.section-header-row');

  let payload = {id: dragId};

  if (item && item.dataset.todoId !== dragId) {
    const targetId = item.dataset.todoId;
    const rect = item.getBoundingClientRect();
    const midY = rect.top + rect.height / 2;
    if (e.clientY < midY) {
      payload.before_id = targetId;
    } else {
      const nextItem = item.nextElementSibling?.closest?.('.todo-item[data-todo-id]')
        || item.nextElementSibling;
      if (nextItem && nextItem.classList.contains('todo-item') && nextItem.dataset.todoId) {
        payload.before_id = nextItem.dataset.todoId;
      } else {
        const targetTodo = allTodos.find(t => t.id === targetId);
        payload.section = targetTodo ? (targetTodo.section || '') : '';
      }
    }
  } else if (sectionHeader) {
    const h3 = sectionHeader.querySelector('h3');
    payload.section = h3 ? h3.textContent : '';
  } else {
    return;
  }

  clearAllDragIndicators();

  await fetch(API + '/drop', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  await loadTodos();
  const ni = visibleIds.indexOf(dragId);
  if (ni >= 0) selectedIdx = ni + 1;
  applySelection();
  dragId = null;
});

/* ── Swipe-right to complete/uncomplete (mobile touch) ── */
(function() {
  if (!('ontouchstart' in window)) return;

  var THRESHOLD_RATIO = 0.30;
  var VELOCITY_THRESHOLD = 0.4; // px/ms

  var startX, startY, startTime, locked, item, content, itemW, isCompleted, todoId, swipeDir;

  function reset() {
    if (item) {
      item.classList.remove('swiping', 'swipe-active', 'swipe-threshold', 'swipe-left', 'snap-back', 'snap-complete');
      if (content) content.style.transform = '';
    }
    startX = startY = startTime = locked = item = content = itemW = isCompleted = todoId = swipeDir = null;
  }

  function findItem(el) {
    while (el && el !== document) {
      if (el.classList && el.classList.contains('todo-item')) return el;
      el = el.parentElement;
    }
    return null;
  }

  function shouldIgnore(target) {
    var tag = (target.tagName || '').toLowerCase();
    return tag === 'button' || tag === 'input' || tag === 'select' || tag === 'textarea' || tag === 'a';
  }

  document.addEventListener('touchstart', function(e) {
    if (e.touches.length !== 1) return;
    var t = e.touches[0];
    var target = e.target;
    if (shouldIgnore(target)) return;
    var el = findItem(target);
    if (!el) return;
    // Bail if item is in edit mode
    if (el.querySelector('.edit-form, .todo-edit')) return;

    item = el;
    content = el.querySelector('.swipe-content');
    if (!content) { item = null; return; }
    todoId = el.getAttribute('data-todo-id');
    itemW = el.offsetWidth;
    isCompleted = el.classList.contains('completed');
    startX = t.clientX;
    startY = t.clientY;
    startTime = Date.now();
    locked = null; // null = undecided, 'h' = horizontal, 'v' = vertical
  }, { passive: true });

  document.addEventListener('touchmove', function(e) {
    if (!item) return;
    var t = e.touches[0];
    var dx = t.clientX - startX;
    var dy = t.clientY - startY;

    if (locked === null) {
      var adx = Math.abs(dx), ady = Math.abs(dy);
      if (adx < 10 && ady < 10) return; // deadzone
      if (ady > adx) { locked = 'v'; reset(); return; }
      locked = 'h';
      item.classList.add('swiping', 'swipe-active');
    }

    if (locked !== 'h') return;
    e.preventDefault();

    // Lock direction on first significant move
    if (!swipeDir) swipeDir = dx > 0 ? 'right' : 'left';

    // Only allow movement in the locked direction
    var absDx = Math.abs(dx);
    if ((swipeDir === 'right' && dx < 0) || (swipeDir === 'left' && dx > 0)) absDx = 0;

    // Toggle swipe-left class for CSS styling (shows delete reveal)
    if (swipeDir === 'left') {
      item.classList.add('swipe-left');
    } else {
      item.classList.remove('swipe-left');
    }

    // Rubber-band past threshold
    var threshold = itemW * THRESHOLD_RATIO;
    var tx;
    if (absDx <= threshold) {
      tx = absDx;
    } else {
      tx = threshold + (absDx - threshold) * 0.4;
    }
    var sign = swipeDir === 'left' ? -1 : 1;
    content.style.transform = 'translateX(' + (tx * sign) + 'px)';

    if (absDx >= threshold) {
      item.classList.add('swipe-threshold');
    } else {
      item.classList.remove('swipe-threshold');
    }
  }, { passive: false });

  document.addEventListener('touchend', function(e) {
    if (!item || locked !== 'h') { reset(); return; }

    var t = e.changedTouches[0];
    var dx = t.clientX - startX;
    var absDx = Math.abs(dx);
    var elapsed = Date.now() - startTime;
    var velocity = absDx / (elapsed || 1);
    var threshold = itemW * THRESHOLD_RATIO;
    var pastThreshold = absDx >= threshold || (velocity > VELOCITY_THRESHOLD && absDx > 30);

    if (pastThreshold && swipeDir) {
      // Animate off-screen
      item.classList.remove('swiping');
      item.classList.add('snap-complete');
      var sign = swipeDir === 'left' ? -1 : 1;
      content.style.transform = 'translateX(' + (itemW * sign) + 'px)';

      var capturedId = todoId;
      var capturedCompleted = isCompleted;
      var capturedItem = item;
      var capturedDir = swipeDir;

      var done = function() {
        capturedItem.removeEventListener('transitionend', done);
        if (capturedDir === 'left') {
          deleteTodo(capturedId);
        } else {
          toggleComplete(capturedId, !capturedCompleted);
        }
      };
      // Listen on the content div for the transform transition
      content.addEventListener('transitionend', done, { once: true });
      // Safety fallback in case transitionend doesn't fire
      setTimeout(function() {
        done();
      }, 350);
    } else {
      // Snap back
      item.classList.remove('swiping', 'swipe-threshold', 'swipe-left');
      item.classList.add('snap-back');
      content.style.transform = '';
      var snapItem = item;
      setTimeout(function() {
        snapItem.classList.remove('snap-back', 'swipe-active');
      }, 300);
    }

    item = null; content = null; locked = null;
  }, { passive: true });

  document.addEventListener('touchcancel', function() {
    if (item) {
      item.classList.remove('swiping', 'swipe-threshold');
      item.classList.add('snap-back');
      if (content) content.style.transform = '';
      var snapItem = item;
      setTimeout(function() {
        snapItem.classList.remove('snap-back', 'swipe-active');
      }, 300);
    }
    item = null; content = null; locked = null;
  }, { passive: true });
})();

let _scrollAnim = null;
let _scrollTarget = null; // the element we're scrolling toward
function scrollIntoViewCentered(el) {
  const viewH = window.innerHeight;
  const pad = viewH * 0.3;
  const rect = el.getBoundingClientRect();
  if (rect.top >= pad && rect.bottom <= viewH - pad) {
    // Already in comfortable zone — cancel any animation and stop
    if (_scrollAnim) { cancelAnimationFrame(_scrollAnim); _scrollAnim = null; }
    _scrollTarget = null;
    return;
  }

  // If already animating toward this element, let it continue
  if (_scrollAnim && _scrollTarget === el) return;

  // Cancel previous animation
  if (_scrollAnim) cancelAnimationFrame(_scrollAnim);
  _scrollTarget = el;

  const duration = 180;
  const t0 = performance.now();

  function step(now) {
    const elapsed = now - t0;
    const progress = Math.min(elapsed / duration, 1);
    // Ease out cubic — fast start, gentle stop
    const eased = 1 - Math.pow(1 - progress, 3);

    // Recalculate target position every frame (tracks element through DOM changes)
    const r = _scrollTarget.getBoundingClientRect();
    const elCenter = r.top + r.height / 2;
    const targetY = window.innerHeight * 0.4;
    const remaining = elCenter - targetY;

    // Lerp: move a fraction of the remaining distance based on eased progress
    window.scrollBy(0, remaining * Math.min(eased * 0.5 + 0.15, 1));

    if (progress < 1 && Math.abs(remaining) > 1) {
      _scrollAnim = requestAnimationFrame(step);
    } else {
      _scrollAnim = null;
      _scrollTarget = null;
    }
  }
  _scrollAnim = requestAnimationFrame(step);
}

function selectedIsSection() {
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return false;
  return visibleIds[selectedIdx - 1].startsWith('__section__:');
}

let _pendingMarkRead = null; // todoId that was opened but not yet marked read

function _flushPendingMarkRead() {
  if (!_pendingMarkRead) return;
  const todoId = _pendingMarkRead;
  _pendingMarkRead = null;
  if (_seenUpdates.has(todoId)) return;
  const t = allTodos.find(x => x.id === todoId);
  if (t && t.title && _parseTitle(t.title).hasUpdatedTag) {
    _seenUpdates.add(todoId);
    _updateSpinnersInPlace();
    fetch(API + '/' + todoId + '/mark-read', { method: 'POST', headers: {'Content-Type': 'application/json'} }).catch(() => {});
  }
}

let _lastSelectedTodoId = null;
let _viewedItems = new Set();

function _clearUnread(todoId) {
  _chatUnread.delete(todoId);
  fetch('/api/chats/' + todoId + '/read', { method: 'POST', headers: {'Content-Type': 'application/json'} }).catch(() => {});
  _updateSpinnersInPlace();
}

function applySelection() {
  // Clear unread on deselect if item was ever expanded
  if (_lastSelectedTodoId && _viewedItems.has(_lastSelectedTodoId)) {
    _viewedItems.delete(_lastSelectedTodoId);
    _clearUnread(_lastSelectedTodoId);
  }

  // Track the current selection
  const curIdx = selectedIdx;
  if (curIdx >= 1 && curIdx <= visibleIds.length) {
    const curId = visibleIds[curIdx - 1];
    _lastSelectedTodoId = curId.startsWith('__section__:') ? null : curId;
  } else {
    _lastSelectedTodoId = null;
  }

  // Collapse previous preview-expanded item
  if (previewExpandedId) {
    const prevEl = document.querySelector(`.todo-item[data-todo-id="${previewExpandedId}"]`);
    if (prevEl) prevEl.classList.remove('preview-expanded');
    previewExpandedId = null;
  }

  // Clear all highlights
  document.querySelectorAll('.todo-item.kb-selected').forEach(el => el.classList.remove('kb-selected'));
  document.querySelectorAll('.section-header-row.kb-selected').forEach(el => el.classList.remove('kb-selected'));
  const form = document.getElementById('add-form');
  form.classList.remove('kb-selected');

  if (selectedIdx === SEL_ADD && addFormVisible) {
    form.classList.add('kb-selected');
    scrollIntoViewCentered(form);
  } else if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
    const curId = visibleIds[selectedIdx - 1];
    if (curId.startsWith('__section__:')) {
      const sec = curId.slice('__section__:'.length);
      const el = document.querySelector(`.section-header-row[data-section="${sec}"]`);
      if (el) {
        el.classList.add('kb-selected');
        scrollIntoViewCentered(el);
      }
    } else {
      const el = document.querySelector(`.todo-item[data-todo-id="${curId}"]`);
      if (el) {
        el.classList.add('kb-selected');
        scrollIntoViewCentered(el);
        // Preview mode: auto-expand if currently collapsed
        if (previewMode) {
          const isExpanded = expandedItems.has(curId);
          if (!isExpanded) {
            el.classList.add('preview-expanded');
            previewExpandedId = curId;
          }
        }
      }
    }
  }
}

document.addEventListener('keydown', e => {
  // Settings dialog: Escape closes it, block all other keys while open
  const settingsOpen = document.getElementById('settings-overlay').classList.contains('visible');
  if (settingsOpen) {
    if (e.key === 'Escape') { e.preventDefault(); hideSettings(); }
    return;
  }
  // History overlay: Escape closes it
  const historyOpen = document.getElementById('history-overlay').style.display === 'flex';
  if (historyOpen) {
    if (e.key === 'Escape') { e.preventDefault(); closeHistoryPanel(); }
    return;
  }
  // Shortcuts dialog: Escape closes it, block all other keys while open
  const shortcutsOpen = document.getElementById('shortcuts-overlay').classList.contains('visible');
  if (shortcutsOpen) {
    if (e.key === 'Escape') { e.preventDefault(); hideShortcuts(); }
    return;
  }

  // Section picker is fully handled by its own input's keydown — skip main handler
  if (sectionPickerOpen) return;

  // Ctrl+number: toggle priority filter (works from anywhere)
  // Option+number: show priority filter; Shift+Option+number: hide priority filter
  const optCodeMap = {'Digit1': 'high', 'Digit2': 'medium', 'Digit3': 'low', 'Digit0': 'none'};
  if (e.altKey && !e.metaKey && !e.ctrlKey && optCodeMap[e.code]) {
    e.preventDefault();
    const p = optCodeMap[e.code];
    if (e.shiftKey) {
      // Hide mode (greyed)
      showPriorities.delete(p);
      hidePriorities.has(p) ? hidePriorities.delete(p) : hidePriorities.add(p);
    } else {
      // Show mode (colored)
      hidePriorities.delete(p);
      showPriorities.has(p) ? showPriorities.delete(p) : showPriorities.add(p);
    }
    selectedIdx = -1;
    render();
    return;
  }

  if (e.altKey && e.key === 'Enter' && !e.metaKey && !e.ctrlKey && !e.shiftKey) {
    e.preventDefault();
    toggleCollapseAll();
    return;
  }

  const tag = (e.target.tagName || '').toLowerCase();

  // When inside the add-form inputs, handle Escape to close form, Cmd+Enter to add
  if (addFormVisible && (tag === 'input' || tag === 'textarea' || tag === 'select')) {
    const inAddForm = e.target.closest('#add-form');
    if (inAddForm) {
      if (e.key === 'Escape') { e.preventDefault(); hideAddForm(); return; }
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); addTodo(); return; }
      return; // Let normal typing work
    }
  }

  // Search input: handle Escape, then let action keys fall through
  const inSearchInput = e.target.id === 'search-input';
  if (inSearchInput) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.target.value = '';
      searchQuery = '';
      e.target.classList.remove('has-query');
      e.target.blur();
      render();
      return;
    }
    // Only ArrowDown/ArrowUp blur and leave search to select items
    // Once an item is selected (selectedIdx >= 1), Enter and other action keys work on it
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      e.target.blur();
      // Fall through to main handler for navigation
    } else if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
      // Item is selected — let Enter, Space, and other item actions through
      const itemActionKeys = new Set(['Enter', ' ']);
      if (itemActionKeys.has(e.key)) {
        e.preventDefault();
        e.target.blur();
        // Fall through to main handler
      } else {
        return; // Stay in search for typing
      }
    } else {
      return; // No item selected, stay in search
    }
  }

  // `/` focuses the search input from anywhere (before the input guard)
  if (e.key === '/' && tag !== 'input' && tag !== 'textarea' && tag !== 'select' && !editingId) {
    e.preventDefault();
    const si = document.getElementById('search-input');
    si.focus();
    si.select();
    return;
  }

  // Ignore when typing in other inputs or editing (but not search — handled above)
  if (!inSearchInput && (tag === 'input' || tag === 'textarea' || tag === 'select')) return;
  if (editingId) return;

  // Filter sessions: Alt+S
  if (e.altKey && e.key === 's' && !e.metaKey && !e.ctrlKey && !e.shiftKey) {
    e.preventDefault();
    toggleFilterSessions();
    return;
  }

  // Filter unread: Alt+U
  if (e.altKey && e.key === 'u' && !e.metaKey && !e.ctrlKey && !e.shiftKey) {
    e.preventDefault();
    toggleFilterUnread();
    return;
  }

  // Undo: Cmd+Z / Ctrl+Z
  if ((e.metaKey || e.ctrlKey) && e.key === 'z' && !e.shiftKey) {
    e.preventDefault();
    performUndo();
    return;
  }

  const maxIdx = visibleIds.length; // 0=add-form, 1..N=todos
  const minIdx = addFormVisible ? SEL_ADD : 1;

  if (e.key === 'ArrowDown' && e.metaKey && !e.shiftKey && !e.altKey) {
    e.preventDefault();
    navigateSection('down');
  } else if (e.key === 'ArrowUp' && e.metaKey && !e.shiftKey && !e.altKey) {
    e.preventDefault();
    navigateSection('up');
  } else if ((e.key === 'ArrowDown' || e.key === 'j' || e.key === 'J') && e.shiftKey && !e.altKey && !e.metaKey) {
    e.preventDefault();
    moveToAdjacentSection('down');
  } else if ((e.key === 'ArrowUp' || e.key === 'k' || e.key === 'K') && e.shiftKey && !e.altKey && !e.metaKey) {
    e.preventDefault();
    moveToAdjacentSection('up');
  } else if (e.key === 'ArrowRight' && e.altKey && !e.metaKey && !e.shiftKey) {
    e.preventDefault();
    showSectionPicker();
  } else if ((e.key === 'ArrowDown' || e.key === 'j') && e.altKey) {
    e.preventDefault();
    if (selectedIsSection()) moveSectionSelected('down');
    else moveSelected('down');
  } else if ((e.key === 'ArrowUp' || e.key === 'k') && e.altKey) {
    e.preventDefault();
    if (selectedIsSection()) moveSectionSelected('up');
    else moveSelected('up');
  } else if (e.key === 'ArrowDown' || e.key === 'j') {
    e.preventDefault();
    if (visibleIds.length === 0 && !addFormVisible) return;
    if (selectedIdx < 0) {
      selectedIdx = minIdx;
    } else {
      selectedIdx = Math.min(selectedIdx + 1, maxIdx);
    }
    applySelection();
  } else if (e.key === 'ArrowUp' || e.key === 'k') {
    e.preventDefault();
    if (visibleIds.length === 0 && !addFormVisible) return;
    selectedIdx = Math.max(selectedIdx - 1, minIdx);
    applySelection();
  } else if (e.key === ' ') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
      e.preventDefault();
      const id = visibleIds[selectedIdx - 1];
      const todo = allTodos.find(t => t.id === id);
      if (todo) toggleComplete(id, todo.status !== 'completed');
    }
  } else if (e.key === 'Enter') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
      e.preventDefault();
      const curId = visibleIds[selectedIdx - 1];
      if (curId.startsWith('__section__:')) {
        const sec = curId.slice('__section__:'.length);
        collapsedSections.delete(sec);
        render();
      } else {
        openChat(curId);
      }
    }
  } else if (e.key === 'e') {
    e.preventDefault();
    if (selectedIdx === SEL_ADD && addFormVisible) {
      document.getElementById('new-title').focus();
    } else if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && selectedIsSection()) {
      const sec = visibleIds[selectedIdx - 1].slice('__section__:'.length);
      startSectionRename(sec);
    } else if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      startEdit(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === 'c' && !e.metaKey && !e.ctrlKey) {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      e.preventDefault();
      copyTodoId(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === 's') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      e.preventDefault();
      openChat(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === '.') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      e.preventDefault();
      startChatBackground(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === 'x') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      e.preventDefault();
      stopChat(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === 'r' && !e.metaKey && !e.ctrlKey) {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      e.preventDefault();
      eaUpdateItem(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === 'h' && !e.metaKey && !e.ctrlKey) {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      e.preventDefault();
      toggleTodoHistory(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === '0' || e.key === '1' || e.key === '2' || e.key === '3') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      e.preventDefault();
      const pMap = {'1': 'high', '2': 'medium', '3': 'low', '0': 'none'};
      changePriority(visibleIds[selectedIdx - 1], pMap[e.key]);
    }
  } else if (e.key === 'p') {
    const sec = getSectionOfSelected();
    if (sec !== null && sec !== '__completed__') {
      e.preventDefault();
      sortByPriority(sec);
    }
  } else if (e.key === 't') {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      e.preventDefault();
      bringToTop(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === 'Backspace' && e.metaKey) {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length && !selectedIsSection()) {
      e.preventDefault();
      deleteTodo(visibleIds[selectedIdx - 1]);
    }
  } else if (e.key === 'ArrowLeft' && !e.metaKey && !e.altKey && !e.shiftKey) {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
      const curId = visibleIds[selectedIdx - 1];
      if (!curId.startsWith('__section__:')) {
        const isExpanded = expandedItems.has(curId);
        if (isExpanded) {
          // Collapse item description
          e.preventDefault();
          toggleItemDesc(curId);
        } else {
          // Collapse the section, select the section marker
          const sec = getSectionOfSelected();
          if (sec !== null && sec !== '' && !collapsedSections.has(sec)) {
            e.preventDefault();
            collapsedSections.add(sec);
            render();
            const markerIdx = visibleIds.indexOf('__section__:' + sec);
            if (markerIdx >= 0) {
              selectedIdx = markerIdx + 1;
              applySelection();
            }
          }
        }
      }
    }
  } else if (e.key === 'ArrowRight' && !e.metaKey && !e.altKey && !e.shiftKey) {
    if (selectedIdx >= 1 && selectedIdx <= visibleIds.length) {
      const curId = visibleIds[selectedIdx - 1];
      if (curId.startsWith('__section__:')) {
        // Expand collapsed section
        e.preventDefault();
        const sec = curId.slice('__section__:'.length);
        collapsedSections.delete(sec);
        render();
      } else {
        // Expand item description
        const isExpanded = expandedItems.has(curId);
        if (!isExpanded) {
          e.preventDefault();
          toggleItemDesc(curId);
        }
      }
    }
  } else if (e.key === '-') {
    e.preventDefault();
    collapseStep();
  } else if (e.key === '+' || e.key === '=') {
    e.preventDefault();
    expandStep();
  } else if (e.key === 'v') {
    e.preventDefault();
    togglePreviewMode();
  } else if (e.key === '?') {
    e.preventDefault();
    showShortcuts();
  } else if (e.key === 'n') {
    e.preventDefault();
    showAddForm();
  } else if (e.key === 'Escape') {
    if (_justCancelledEdit) { _justCancelledEdit = false; return; }
    e.preventDefault();
    if (document.getElementById('terminal-overlay').classList.contains('visible')) {
      minimizeTerminal(); return;
    }
    if (addFormVisible) { hideAddForm(); }
    else if (searchQuery.trim().length > 0 || showPriorities.size > 0 || hidePriorities.size > 0) {
      searchQuery = '';
      showPriorities.clear();
      hidePriorities.clear();
      const searchEl = document.getElementById('search-input');
      searchEl.value = '';
      searchEl.classList.remove('has-query');
      render();
    }
    else { selectedIdx = -1; applySelection(); }
  }
});

// --- Search input ---
document.getElementById('search-input').addEventListener('input', e => {
  searchQuery = e.target.value;
  e.target.classList.toggle('has-query', searchQuery.trim().length > 0);
  selectedIdx = -1;
  render();
});

// --- Section picker (Opt+Right) ---
let sectionPickerOpen = false;
let sectionPickerIdx = 0;
let spAllItems = [];      // full unfiltered list [{name, label, isCurrent}]
let spFilteredItems = [];  // after fuzzy filter
let spLastQuery = '';      // persists across open/close
let sectionPickerTodoId = null;

function spFuzzyMatch(query, text) {
  // Simple fuzzy: every char of query appears in order in text (case-insensitive)
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  let qi = 0;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) qi++;
  }
  return qi === q.length;
}

function spFuzzyScore(query, text) {
  // Lower = better. Prioritize: exact prefix > substring > fuzzy
  const q = query.toLowerCase();
  const t = text.toLowerCase();
  if (t.startsWith(q)) return 0;
  if (t.includes(q)) return 1;
  return 2;
}

function showSectionPicker() {
  if (selectedIdx < 1 || selectedIdx > visibleIds.length) return;
  const id = visibleIds[selectedIdx - 1];
  const todo = allTodos.find(t => t.id === id);
  if (!todo || todo.status === 'completed') return;
  const curSection = todo.section || '';

  const allSections = [];
  const seen = new Set();
  allTodos.forEach(t => {
    const s = t.section || '';
    if (s && !seen.has(s)) { allSections.push(s); seen.add(s); }
  });
  spAllItems = [{name: '', label: '(No section)', isCurrent: curSection === ''}];
  allSections.forEach(s => {
    spAllItems.push({name: s, label: s, isCurrent: s === curSection});
  });

  sectionPickerTodoId = id;
  sectionPickerOpen = true;

  // Restore last query and filter accordingly
  if (spLastQuery) {
    spFilteredItems = spAllItems
      .filter(x => !x.isCurrent && spFuzzyMatch(spLastQuery, x.label))
      .sort((a, b) => spFuzzyScore(spLastQuery, a.label) - spFuzzyScore(spLastQuery, b.label));
  } else {
    spFilteredItems = spAllItems.filter(x => !x.isCurrent);
  }
  sectionPickerIdx = 0;

  renderSectionPicker();

  // Position near the selected todo item
  const el = document.querySelector(`.todo-item[data-todo-id="${id}"]`);
  const picker = document.getElementById('section-picker');
  if (el) {
    const rect = el.getBoundingClientRect();
    let x = rect.right - 240;
    let y = rect.top + rect.height + 4;
    if (x < 8) x = 8;
    if (y + 220 > window.innerHeight) y = rect.top - picker.offsetHeight - 4;
    picker.style.left = x + 'px';
    picker.style.top = y + 'px';
  }

  // Focus input after render, select all text so user can type to replace
  setTimeout(() => {
    const inp = document.getElementById('sp-input');
    if (inp) {
      inp.focus();
      if (spLastQuery) inp.select();
    }
  }, 0);
}

function renderSectionPicker() {
  const picker = document.getElementById('section-picker');
  const existingInput = document.getElementById('sp-input');
  const inputVal = existingInput ? existingInput.value : spLastQuery;

  let itemsHtml = '';
  if (spFilteredItems.length === 0 && inputVal.trim()) {
    itemsHtml = `<div class="section-picker-item sp-selected" onmousedown="spCommitNew()">Create &ldquo;${esc(inputVal.trim())}&rdquo;</div>`;
  } else {
    itemsHtml = spFilteredItems.map((item, i) => {
      const cls = ['section-picker-item'];
      if (i === sectionPickerIdx) cls.push('sp-selected');
      return `<div class="${cls.join(' ')}" data-sp-idx="${i}" onmousedown="spCommitIdx(${i})">${esc(item.label)}</div>`;
    }).join('');
  }

  picker.innerHTML =
    `<input type="text" class="section-picker-input" id="sp-input" placeholder="Search or create section..." autocomplete="off" value="${esc(inputVal)}">`
    + itemsHtml;
  picker.classList.add('visible');

  // Restore cursor position and set up events
  const input = document.getElementById('sp-input');
  input.setSelectionRange(inputVal.length, inputVal.length);
  input.addEventListener('input', spOnInput);
  input.addEventListener('keydown', spOnKeydown);
}

function spOnInput(e) {
  const query = e.target.value.trim();
  spLastQuery = e.target.value;
  if (!query) {
    spFilteredItems = spAllItems.filter(x => !x.isCurrent);
  } else {
    spFilteredItems = spAllItems
      .filter(x => !x.isCurrent && spFuzzyMatch(query, x.label))
      .sort((a, b) => spFuzzyScore(query, a.label) - spFuzzyScore(query, b.label));
  }
  sectionPickerIdx = 0;
  spUpdateItems();
}

function spUpdateItems() {
  // Re-render just the items, not the input (preserves focus/cursor)
  const picker = document.getElementById('section-picker');
  const input = document.getElementById('sp-input');
  const inputVal = input?.value || '';

  // Remove old items (everything after the input)
  while (picker.lastChild && picker.lastChild !== input) {
    picker.removeChild(picker.lastChild);
  }

  if (spFilteredItems.length === 0 && inputVal.trim()) {
    const div = document.createElement('div');
    div.className = 'section-picker-item sp-selected';
    div.innerHTML = `Create &ldquo;${esc(inputVal.trim())}&rdquo;`;
    div.onmousedown = () => spCommitNew();
    picker.appendChild(div);
  } else {
    spFilteredItems.forEach((item, i) => {
      const div = document.createElement('div');
      div.className = 'section-picker-item' + (i === sectionPickerIdx ? ' sp-selected' : '');
      div.textContent = item.label;
      div.onmousedown = () => spCommitIdx(i);
      picker.appendChild(div);
    });
  }
}

function spOnKeydown(e) {
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    if (spFilteredItems.length > 0) {
      sectionPickerIdx = Math.min(sectionPickerIdx + 1, spFilteredItems.length - 1);
      spUpdateItems();
    }
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    if (spFilteredItems.length > 0) {
      sectionPickerIdx = Math.max(sectionPickerIdx - 1, 0);
      spUpdateItems();
    }
  } else if (e.key === 'Enter') {
    e.preventDefault();
    const inputVal = e.target.value.trim();
    if (spFilteredItems.length > 0) {
      spCommitIdx(sectionPickerIdx);
    } else if (inputVal) {
      spCommitNew();
    } else {
      hideSectionPicker();
    }
  } else if (e.key === 'Escape') {
    e.preventDefault();
    hideSectionPicker();
  } else if (e.key === 'ArrowLeft') {
    if (!e.target.value) {
      e.preventDefault();
      hideSectionPicker();
    }
  }
  e.stopPropagation();
}

function hideSectionPicker() {
  sectionPickerOpen = false;
  sectionPickerTodoId = null;
  document.getElementById('section-picker').classList.remove('visible');
}

async function spMoveTo(sectionName) {
  const id = sectionPickerTodoId;
  const prevIdx = selectedIdx;
  hideSectionPicker();
  if (!id) return;

  await fetch(API + '/drop', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id, section: sectionName})
  });
  await fetch(API + '/move-to-top', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id})
  });

  await loadTodos();
  selectedIdx = Math.min(prevIdx, visibleIds.length);
  if (selectedIdx < 1 && visibleIds.length > 0) selectedIdx = 1;
  applySelection();
}

function spCommitIdx(idx) {
  const item = spFilteredItems[idx];
  if (!item) { hideSectionPicker(); return; }
  spMoveTo(item.name);
}

function spCommitNew() {
  const input = document.getElementById('sp-input');
  const name = input?.value.trim();
  if (name) spMoveTo(name);
  else hideSectionPicker();
}

// --- Section rename ---
let renamingSection = null;

function startSectionRename(sectionName) {
  renamingSection = sectionName;
  const row = document.querySelector(`.section-header-row[data-section="${CSS.escape(sectionName)}"]`);
  if (!row) return;
  const h3 = row.querySelector('h3');
  if (!h3) return;
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'section-rename-input';
  input.value = sectionName;
  h3.replaceWith(input);
  input.focus();
  input.select();

  function commit() {
    const newName = input.value.trim();
    if (newName && newName !== sectionName) {
      saveSectionRename(sectionName, newName);
    } else {
      renamingSection = null;
      render();
    }
  }

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { e.preventDefault(); renamingSection = null; render(); }
  });
  input.addEventListener('blur', () => {
    setTimeout(() => { if (renamingSection === sectionName) commit(); }, 100);
  });
}

async function saveSectionRename(oldName, newName) {
  renamingSection = null;
  await fetch('/api/sections/rename', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({old_name: oldName, new_name: newName})
  });
  // Update collapsed sections set if the renamed section was collapsed
  if (collapsedSections.has(oldName)) {
    collapsedSections.delete(oldName);
    collapsedSections.add(newName);
  }
  loadTodos();
}

loadTodos();
startPolling();
pollJobs();
// Load initial unread state (once — not polled)
fetch('/api/chats/unread').then(r => r.json()).then(ids => { _chatUnread = new Set(ids); _updateSpinnersInPlace(); }).catch(() => {});

// Fade section headers and items behind them as they get covered by the next sticky header
window.addEventListener('scroll', () => {
  const offset = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--section-offset')) || 0;
  const headers = [...document.querySelectorAll('.section-header-row')];
  for (let i = 0; i < headers.length; i++) {
    const h = headers[i];
    const rect = h.getBoundingClientRect();
    const isStuck = rect.top <= offset + 1;
    if (!isStuck) { h.style.opacity = ''; continue; }
    const next = headers[i + 1];
    if (!next) { h.style.opacity = ''; continue; }
    const nextTop = next.getBoundingClientRect().top;
    const dist = nextTop - offset;
    const hh = h.offsetHeight;
    const fadeZone = hh * 1.5;
    let fade;
    if (dist <= 0) fade = '0';
    else if (dist < fadeZone) fade = (dist / fadeZone).toFixed(2);
    else fade = '';
    h.style.opacity = fade;
  }
}, { passive: true });

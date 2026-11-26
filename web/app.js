'use strict';

// ---------------------------------------------------------------- 状态
const S = {
  tasks: [],
  running: false,
  stopping: false,
  recording: null,
  screen: [0, 0],
  logSeq: 0,
  selected: new Set(),
  busy: false,          // 正在处理一次点击
  connected: false,
  preview: false,       // 画面预览开关（网页版默认关）
  settled: false,       // 画面是否已经做过「加载后一次性对齐」
  rendered: '',         // 已渲染的任务 key 指纹
  dragging: '',         // 正在拖动的任务 key
  alertTs: 0,           // 已经弹过的提示时间戳
  orderFile: 'tasks/order.json',   // 排序文件路径（服务端给，打包后在 exe 目录）
  update: null,         // 服务端给的最新版本/下载进度
};

// 上一次渲染录制面板时用的 key。换了一次录制才回填输入框，
// 否则状态轮询每 1.6 秒会把用户正在打的任务名冲掉（还会打断中文输入法）。
let recSig = '';

const $ = (id) => document.getElementById(id);
const img = $('screen');
const wrap = $('screenWrap');
const logBox = $('log');

// ---------------------------------------------------------------- 工具
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

let toastTimer = 0;
function toast(msg, isErr, ms) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast' + (isErr ? ' err' : '');
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, ms || 2400);
}

// ---------------------------------------------------------------- 勾选持久化
// 以前只存 localStorage：那是按「来源」隔离的，端口一变（8765 被占就换 8766）
// 就等于换了新域，勾选全丢；重新打包 exe 也带不走。现在以服务端
// tasks/selection.json 为准，localStorage 只当启动时的兜底。
let selLoaded = false;   // 服务端那份只回填一次，之后以界面上的操作为准

function loadSelected() {
  try {
    const raw = localStorage.getItem('selected');
    if (raw) S.selected = new Set(JSON.parse(raw));
  } catch (e) { /* 忽略 */ }
}
function saveSelected() {
  const keys = [...S.selected];
  localStorage.setItem('selected', JSON.stringify(keys));
  api('/api/tasks/select', { keys }).catch(() => { /* 存不上就下次再说 */ });
}

// 服务端有记录就用它；第一次运行（没这个文件）才退回 localStorage / 默认全选
function applySelectionFromServer(state) {
  if (selLoaded) return;
  selLoaded = true;
  if (Array.isArray(state.selection)) {
    S.selected = new Set(state.selection.map(String));
    localStorage.setItem('selected', JSON.stringify([...S.selected]));
    return;
  }
  applyDefaultSelection(state.tasks);
}

// 第一次打开时默认勾选所有模板齐全的任务
function applyDefaultSelection(tasks) {
  if (localStorage.getItem('selected') !== null) return;
  S.selected = new Set(
    tasks.filter((t) => !(t.missing && t.missing.length)).map((t) => t.key));
  saveSelected();
}

// ---------------------------------------------------------------- 画面
// 网页版默认不开画面预览：游戏本身就在浏览器窗口里摆着，
// 再镜像一份纯属白烧 CPU（服务端要多解码、多编码，浏览器要多解一张大图）。
// 需要时点工具栏的「画面」按钮，或者在录制时自动打开。
let streamOn = false;
let streamTries = 0;

function startStream() {
  if (streamOn) return;
  streamOn = true;
  img.src = '/api/stream?t=' + Date.now();
}

function stopStream() {
  streamOn = false;
  img.removeAttribute('src');
}

img.addEventListener('load', () => {
  streamTries = 0;
  $('screenTip').classList.add('hide');
  setConnected(true);
});

img.addEventListener('error', () => {
  // 流断了：服务重启、窗口关了、或者刚启动还没出帧。退避重连。
  streamOn = false;
  if (!S.preview) return;              // 预览关着时的 204 不是错误
  streamTries = Math.min(streamTries + 1, 6);
  if (!S.running) {
    $('screenTip').classList.remove('hide');
    $('screenTip').textContent = '没拿到画面 · 看下面日志里卡在哪一步';
  }
  setTimeout(startStream, 400 * streamTries);
});

// 只取一帧（截图用），不进循环
async function fetchOnce() {
  try {
    const r = await fetch('/api/frame');
    if (!r.ok) throw new Error('no frame');
    img.src = URL.createObjectURL(await r.blob());
    $('screenTip').classList.add('hide');
    setConnected(true);
  } catch (e) {
    setConnected(false);
    $('screenTip').classList.remove('hide');
  }
}

function setConnected(ok) {
  S.connected = ok;
  $('dot').className = 'dot ' + (ok ? (S.running ? 'busy' : 'on') : 'off');
  $('connText').textContent = ok ? '已连接' : '未连接';
  $('screenSize').textContent = ok && S.screen[0]
    ? `${S.screen[0]}×${S.screen[1]}` : '—';
}

// ---------------------------------------------------------------- 日志
function appendLog(records) {
  const nearBottom = logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 60;
  const frag = document.createDocumentFragment();
  for (const r of records) {
    const row = document.createElement('div');
    row.className = 'row ' + r.level;
    const t = document.createElement('span');
    t.className = 't';
    t.textContent = r.t;
    const m = document.createElement('span');
    m.className = 'm';
    m.textContent = r.msg;
    row.append(t, m);
    frag.append(row);
  }
  logBox.append(frag);
  while (logBox.childElementCount > 800) logBox.firstElementChild.remove();
  if (nearBottom) logBox.scrollTop = logBox.scrollHeight;
}

async function logLoop() {
  while (true) {
    try {
      const d = await api(`/api/log?since=${S.logSeq}`);
      if (d.records && d.records.length) appendLog(d.records);
      S.logSeq = d.seq;
    } catch (e) { /* 服务重启时忽略 */ }
    await sleep(550);
  }
}

// ---------------------------------------------------------------- 拖动排序
// 用 HTML5 拖放：只有按在左边的序号上才开始拖（不然会和勾选、双击改名打架）。
// 拖动过程中直接挪 DOM，松手时把整份顺序发给服务端存起来。
function renumber() {
  const items = $('taskList').querySelectorAll('.task');
  items.forEach((li, i) => {
    const ord = li.querySelector('.ord');
    if (ord) ord.textContent = String(i + 1);
  });
}

async function persistOrder() {
  const keys = [...$('taskList').querySelectorAll('.task')].map((li) => li.dataset.key);
  renumber();
  // 先把指纹对齐成新顺序：服务端认了就不会触发重建，界面不会跳一下
  S.rendered = keys.join('|');
  try {
    const r = await api('/api/tasks/order', { keys });
    if (!r.ok) {
      toast(r.hint || '顺序没保存', true);
      S.rendered = '';
      refreshState();
    }
  } catch (e) {
    toast('顺序没保存：' + e.message, true);
    S.rendered = '';
    refreshState();
  }
}

// ---------------------------------------------------------------- 按名字排序
// 任务多了以后拖序号很累（还要滚半天），这里给一份纯文本顺序：一行一个任务名。
// 名字认不出来就跳过，没写到的任务排到最后 —— 不会因为手抖把清单清空。
// 真正落盘还是 tasks/order.json，所以两种改法等价。
function openOrderDlg() {
  if (S.running) return toast('正在执行，等这轮跑完再调顺序', true);
  if (!S.tasks.length) return toast('还没有任务', true);
  $('orderText').value = S.tasks.map((t) => t.name).join('\n');
  $('orderPath').textContent = S.orderFile;
  $('orderDlg').hidden = false;
  const ta = $('orderText');
  ta.focus();
  ta.setSelectionRange(0, 0);
}

function closeOrderDlg() {
  $('orderDlg').hidden = true;
}

async function saveOrderText() {
  const lines = $('orderText').value
    .split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  const byName = new Map();
  const byKey = new Map();
  for (const t of S.tasks) {
    byName.set(t.name.trim(), t.key);
    byKey.set(t.key, t.key);
  }
  const keys = [];
  const unknown = [];
  for (const ln of lines) {
    const k = byKey.get(ln) || byName.get(ln);
    if (!k) { unknown.push(ln); continue; }
    if (!keys.includes(k)) keys.push(k);
  }
  if (!keys.length) {
    return toast('一个任务名都没认出来，顺序没改', true);
  }
  for (const t of S.tasks) if (!keys.includes(t.key)) keys.push(t.key);

  try {
    const r = await api('/api/tasks/order', { keys });
    if (!r.ok) return toast(r.hint || '顺序没保存', true);
    S.rendered = '';
    closeOrderDlg();
    if (unknown.length) {
      toast(`已保存，${unknown.length} 行没认出来：${unknown[0]}`.slice(0, 60), true, 4200);
    } else {
      toast('顺序已保存');
    }
    refreshState();
  } catch (e) {
    toast('顺序没保存：' + e.message, true);
  }
}


// 有选项的任务（救援战这类）在名字下面多一行小药丸：属性、难度各一排。
// 「全部」= 不筛选：清单里第一个能参加的就打。
// 勾选存到服务端（tasks/options.json），下次打开、换个 exe 都还在。
function buildOptions(li, t) {
  const box = document.createElement('div');
  box.className = 'opts';
  const state = new Map();     // 多选选项：Set
  const numbers = new Map();   // 数值选项：字符串
  const lines = [];            // 带 show_when 的选项行，按依赖关系显隐
  for (const o of t.options) {
    const chosen = (t.selected && t.selected[o.key]) || o.default || [];
    if (o.type === 'number' || o.type === 'text') {
      numbers.set(o.key, String(chosen[0] ?? o.default?.[0] ?? ''));
    } else {
      state.set(o.key, new Set(chosen));
    }
  }

  // 选项之间的显隐依赖：o.show_when = {key: "modes", values: ["manual"]}
  // 比如「方式=跳过」时，目标王/难度/加护/重刷上限这些手动专属的就该藏起来。
  const applyShow = () => {
    for (const it of lines) {
      const w = it.o.show_when;
      if (!w) continue;
      const s = state.get(w.key);
      const on = !!(s && (w.values || []).some((v) => s.has(v)));
      it.line.style.display = on ? '' : 'none';
    }
  };

  const save = async () => {
    const values = {};
    for (const [k, v] of state) values[k] = [...v];
    for (const [k, v] of numbers) values[k] = [v];
    applyShow();
    try {
      const r = await api('/api/tasks/options', { key: t.key, values });
      if (!r.ok) {
        toast(r.hint || '选项没保存', true);
        S.rendered = '';
        refreshState();
      }
    } catch (e) {
      toast('选项没保存：' + e.message, true);
    }
  };

  const paint = (o, chips, allChip) => {
    const set = state.get(o.key);
    const every = o.choices.length;
    for (const [v, el] of chips) el.classList.toggle('on', set.has(v));
    if (o.single) { allChip.style.display = 'none'; return; }
    const all = set.size >= every;
    allChip.classList.toggle('on', all);
    allChip.textContent = all ? '全部' : '全部';
  };

  for (const o of t.options) {
    const line = document.createElement('div');
    line.className = 'opt-line';
    if (o.show_when) lines.push({ o, line });
    const lb = document.createElement('span');
    lb.className = 'opt-label';
    lb.textContent = o.label;
    if (o.hint) lb.title = o.hint;
    line.append(lb);

    // 数值型：渲染成数字输入框（扫荡次数这类）
    if (o.type === 'number') {
      const input = document.createElement('input');
      input.type = 'number';
      input.className = 'onum';
      input.min = o.min ?? 1;
      input.max = o.max ?? 99;
      input.value = numbers.get(o.key) ?? (o.default?.[0] ?? 1);
      if (o.hint) input.title = o.hint;
      const commit = () => {
        let n = parseInt(input.value, 10);
        if (isNaN(n)) n = parseInt(o.default?.[0] ?? 1, 10);
        n = Math.max(o.min ?? 1, Math.min(o.max ?? 99, n));
        input.value = n;
        numbers.set(o.key, String(n));
        save();
      };
      input.onchange = commit;
      input.onblur = commit;
      line.append(input);
      box.append(line);
      continue;
    }

    // 文本型：渲染成文本输入框（章节 16-3-3 这类）
    if (o.type === 'text') {
      const input = document.createElement('input');
      input.type = 'text';
      input.className = 'onum otext';
      input.value = numbers.get(o.key) ?? (o.default?.[0] ?? '');
      if (o.hint) input.title = o.hint;
      const commit = () => {
        const v = String(input.value || '').trim() || String(o.default?.[0] ?? '');
        input.value = v;
        numbers.set(o.key, v);
        save();
      };
      input.onchange = commit;
      input.onblur = commit;
      line.append(input);
      box.append(line);
      continue;
    }

    const allChip = document.createElement('button');
    allChip.type = 'button';
    allChip.className = 'ochip all';
    allChip.title = o.hint || '不筛选';
    allChip.textContent = '全部';
    allChip.onclick = (e) => {
      e.stopPropagation();
      const set = state.get(o.key);
      set.clear();
      for (const c of o.choices) set.add(c.v);
      paintAll();
      save();
    };

    line.append(lb, allChip);
    if (o.single) allChip.style.display = 'none';   // 单选没有"全部"这一说

    const chips = [];
    for (const c of o.choices) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ochip';
      b.textContent = c.label;
      b.onclick = (e) => {
        e.stopPropagation();
        const set = state.get(o.key);
        // 单选选项（方式/难度/加护这类"只能挑一个"的）：点了就替换，不能取消成空
        if (o.single) {
          set.clear();
          set.add(c.v);
          paintAll();
          save();
          return;
        }
        const all = set.size >= o.choices.length;
        if (set.has(c.v)) {
          // 全选状态下点某一项 = 只看这一项（和游戏里的筛选弹窗一个脾气）
          if (all) {
            set.clear();
            set.add(c.v);
          } else {
            set.delete(c.v);
            if (!set.size) for (const x of o.choices) set.add(x.v);
          }
        } else {
          set.add(c.v);
        }
        paintAll();
        save();
      };
      chips.push([c.v, b]);
      line.append(b);
    }
    box.append(line);

    const paintAll = () => paint(o, chips, allChip);
    paintAll();
  }

  // 选项条自己吃掉点击，别让整行的「点一下切换勾选」被误触
  applyShow();                 // 首次渲染就按当前「方式」决定哪些选项要显示
  box.onclick = (e) => e.stopPropagation();
  box.ondblclick = (e) => e.stopPropagation();
  return box;
}

// ---------------------------------------------------------------- 任务列表
function renderTasks(state) {
  const list = $('taskList');
  const taskMap = new Map(state.tasks.map((t) => [t.key, t]));

  // 清理已经不存在的勾选
  let pruned = false;
  for (const k of [...S.selected]) {
    if (!taskMap.has(k)) { S.selected.delete(k); pruned = true; }
  }
  if (pruned) saveSelected();

  const fingerprint = state.tasks.map((t) => t.key).join('|');
  if (fingerprint !== S.rendered) {
    S.rendered = fingerprint;
    list.textContent = '';
    state.tasks.forEach((t, idx) => {
      const li = document.createElement('li');
      li.className = 'task';
      li.dataset.key = t.key;

      // 左边这个序号既是「第几个跑」的提示，也是拖动的把手
      const ord = document.createElement('span');
      ord.className = 'ord';
      ord.textContent = String(idx + 1);
      ord.title = '按住拖动，调整执行顺序';
      ord.onclick = (e) => e.stopPropagation();
      ord.onmousedown = () => {
        if (S.running) {
          toast('正在执行，等这轮跑完再调顺序', true);
          return;
        }
        li.draggable = true;
      };
      ord.onmouseup = () => { li.draggable = false; };

      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = S.selected.has(t.key);

      const meta = document.createElement('div');
      meta.className = 'meta';
      const nm = document.createElement('div');
      nm.className = 'name';
      nm.textContent = t.name;
      if (t.deletable) {
        nm.title = '双击改名';
        nm.ondblclick = (e) => {
          e.stopPropagation();
          e.preventDefault();
          startRename(li, t);
        };
      }
      const ds = document.createElement('div');
      ds.className = 'desc';
      ds.textContent = t.source === 'recipe'
        ? `录制 · ${t.needs.length} 个模板`
        : `内置 · ${t.needs.length} 个模板`;
      meta.append(nm, ds);

      li.append(ord, cb, meta);
      if (t.options && t.options.length) li.append(buildOptions(li, t));

      li.addEventListener('dragstart', (e) => {
        li.classList.add('dragging');
        S.dragging = t.key;
        if (e.dataTransfer) {
          e.dataTransfer.effectAllowed = 'move';
          try { e.dataTransfer.setData('text/plain', t.key); } catch (err) { /* IE 那套兼容不用管 */ }
        }
      });
      li.addEventListener('dragend', () => {
        li.classList.remove('dragging');
        li.draggable = false;
        S.dragging = '';
        persistOrder();
      });
      li.addEventListener('dragover', (e) => {
        const dragged = list.querySelector('.task.dragging');
        if (!dragged || dragged === li) return;
        e.preventDefault();                 // 不拦这一下，drop 不会被触发
        const r = li.getBoundingClientRect();
        const after = e.clientY > r.top + r.height / 2;
        list.insertBefore(dragged, after ? li.nextSibling : li);
      });

      if (t.deletable) {
        const del = document.createElement('button');
        del.className = 'del';
        del.textContent = '×';
        del.title = '删除这个任务';
        del.onclick = async (e) => {
          e.stopPropagation();
          if (!confirm(`删除任务「${t.name}」？模板文件会保留。`)) return;
          const r = await api('/api/tasks/delete', { key: t.key });
          if (r.ok) { toast('已删除'); refreshState(); }
          else toast(r.hint || '删除失败', true);
        };
        li.append(del);
      }

      li.onclick = (e) => {
        if (e.target === cb || e.target.tagName === 'INPUT') return;
        // 两次点击都切换勾选 —— 双击改名时净效果为 0，不会误改勾选状态
        cb.checked = !cb.checked;
        cb.dispatchEvent(new Event('change'));
      };
      cb.onchange = () => {
        if (cb.checked) S.selected.add(t.key);
        else S.selected.delete(t.key);
        li.classList.toggle('sel', cb.checked);
        saveSelected();
      };
      li.classList.toggle('sel', cb.checked);
      list.append(li);
    });
  }

  // 更新状态徽标与缺模板提示
  for (const t of state.tasks) {
    const li = list.querySelector(`[data-key="${t.key}"]`);
    if (!li) continue;
    let badge = li.querySelector('.badge');
    li.classList.toggle('running', t.status === 'running');
    const label = { pending: '等待', running: '执行中', ok: '完成', fail: '失败', skip: '跳过' }[t.status];
    if (label) {
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'badge';
        li.append(badge);
      }
      badge.className = 'badge ' + t.status;
      badge.textContent = label;
      badge.title = t.status_note || label;
    } else if (badge) {
      badge.remove();
    }
    const ds = li.querySelector('.desc');
    if (ds) {
      if (t.missing && t.missing.length) {
        ds.className = 'desc warn';
        ds.textContent = `缺 ${t.missing.length} 个模板：${t.missing.slice(0, 2).join(', ')}…`;
      } else {
        ds.className = 'desc';
        ds.textContent = t.source === 'recipe'
          ? `录制 · ${t.needs.length} 个模板`
          : `内置 · ${t.needs.length} 个模板`;
      }
    }
  }
}

function refreshState() {
  return api('/api/state').then(applyState).catch(() => {});
}

// ---------------------------------------------------------------- 任务改名
// 双击任务名就地变输入框。Enter 保存，Esc 取消，点别处也算保存。
function startRename(li, t) {
  if (!li || li.querySelector('.name-edit')) return;
  const nm = li.querySelector('.name');
  if (!nm) return;

  const input = document.createElement('input');
  input.className = 'name-edit';
  input.value = t.name;
  input.maxLength = 40;
  input.autocomplete = 'off';
  nm.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    const v = input.value.trim();
    if (commit && v && v !== t.name) {
      try {
        const r = await api('/api/tasks/rename', { key: t.key, name: v });
        if (!r.ok) toast(r.hint || '改名失败', true);
        else toast(`改名为「${v}」`);
      } catch (e) {
        toast('改名失败：' + e.message, true);
      }
    }
    S.rendered = '';          // 强制重绘任务列表
    refreshState();
  };

  input.onkeydown = (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  };
  input.onclick = (e) => e.stopPropagation();
  input.ondblclick = (e) => e.stopPropagation();
  input.onblur = () => finish(true);
}

async function stateLoop() {
  while (true) {
    try { applyState(await api('/api/state')); } catch (e) { /* 忽略 */ }
    await sleep(1600);
  }
}

function applyState(st) {
  S.running = st.running;
  S.recording = st.recording;
  S.connected = st.connected;
  // 这一份以前没人赋值：S.tasks 一直是空数组，[].every() 恒为 true，
  // 于是「全选」以为已经全勾了、反而把勾选清空 —— 看着就是点了没反应。
  S.tasks = Array.isArray(st.tasks) ? st.tasks : [];
  if (st.order_file) S.orderFile = st.order_file;
  if (st.device && st.device[0]) S.screen = st.device;
  else if (st.screen) S.screen = st.screen;
  if (st.game) $('gameName').textContent = st.game;
  if (st.update) renderUpdate(st.update);

  // 任务跑的时候喊话（清单刷不出战、BP 不足、BOSS 被抢…）要让人看见，
  // 不能只躺在下面的日志里。按 ts 判断是不是新的，避免每次轮询重弹；
  // 超过 2 分钟的老消息也不再弹（刷新页面时不该被上一次的提示糊一脸）。
  const alert = st.alert || {};
  if (alert.ts && alert.ts !== S.alertTs) {
    S.alertTs = alert.ts;
    if (alert.msg && Date.now() / 1000 - alert.ts < 120) toast(alert.msg, true, 9000);
  }

  // 「帧率」在网页版报的是浏览器 screencast 的实时帧率（识别就吃这路流）
  const cap = st.capture || {};
  S.capture = cap;
  const rate = cap.fps || 0;
  $('fps').textContent = cap.ok && rate ? `${Math.round(rate)} fps` : '—';
  const fpsMode = cap.fps_mode || 'auto';
  const tagText = { auto: '自动', high: '60', low: '省电' }[fpsMode] || '自动';
  $('fpsModeTag').textContent = tagText;
  const target = cap.target_fps || 0;
  $('btnFps').title = `帧率模式：${tagText}（点击切换 自动 → 60 → 省电）`
    + (target ? `\n目标 ${target}fps（高 ${cap.fps_high || 60} / 省电 ${cap.fps_low || 20}）` : '')
    + '\n自动 = 画面预览开时用 60，关时降到省电';
  $('fps').title = cap.ok
    ? `实时 ${Math.round(rate)} fps${target ? ` / 目标 ${target}` : ''} · 视口 ${(st.viewport || []).join('×')}`
      + ` · 识别 ${st.perf ? st.perf.avg_ms : 0}ms/次`
    : '画面流未就绪';

  // 画面预览开关：默认关（游戏就在旁边的窗口里看着），录制时强制开
  const wantPreview = !!st.preview || !!st.recording;
  S.preview = wantPreview;
  $('btnPreview').classList.toggle('on', wantPreview);
  $('btnPreview').textContent = wantPreview ? '画面 开' : '画面 关';
  S.settled = !!st.settled;
  wrap.classList.toggle('nopreview', !wantPreview);
  if (wantPreview && !streamOn) startStream();
  if (!wantPreview && streamOn) stopStream();

  // 状态提示：没登录、还在加载、连不上，都直接写到画面上
  const tip = $('screenTip');
  if (st.status === 'need_login') {
    tip.classList.remove('hide');
    // hint 里会带登录错误码（如 1002），比一句「请登录」有用得多
    tip.textContent = (st.hint || '请在浏览器窗口里登录一次（登录状态会记住）')
      + (st.login_err ? '' : ' · 登不进去就点「登录令牌」粘 cookie');
  } else if (st.hint && !st.connected) {
    tip.classList.remove('hide');
    tip.textContent = st.hint;
  } else if (!wantPreview) {
    tip.classList.remove('hide');
    tip.textContent = '画面预览已关闭（点「画面」开启）· 游戏在旁边的浏览器窗口里';
  }

  applySelectionFromServer(st);
  renderTasks(st);

  S.stopping = !!st.stopping;
  $('btnRun').disabled = st.running || S.recording !== null;
  // 停止中把按钮留着但改文案：用户能看出「已经收到了」，不是点了没反应
  $('btnStop').disabled = !st.running;
  $('btnStop').textContent = st.stopping ? '■ 停止中…' : '■ 停止';
  $('summary').textContent = st.last_summary || '';
  $('dot').className = 'dot ' + (st.connected ? (st.running ? 'busy' : 'on') : 'off');
  $('connText').textContent = st.stopping
    ? '正在停止…'
    : st.running
    ? (st.tasks.find((t) => t.key === st.current)?.name || '执行中')
    : (st.connected
      ? (st.status === 'ready' && !st.settled ? '已连接 · 等画面稳定后对齐' : '已连接')
      : '未连接');

  $('viewTasks').hidden = !!st.recording;
  $('viewRec').hidden = !st.recording;
  wrap.classList.toggle('rec', !!st.recording);

  if (st.recording) {
    // 只在「换了一次录制」时回填。否则每 1.6 秒一次的状态轮询会把正在输入的字冲掉，
    // 而且给 input.value 赋值会让光标跳到末尾、打断中文输入法 —— 表现就是「名字改不了」。
    const sig = st.recording.key;
    if (sig !== recSig) {
      recSig = sig;
      // 双保险：正在这个框里打字时，一个字都不许动
      const el = document.activeElement;
      if (el !== $('recName')) $('recName').value = st.recording.name || '';
      if (el !== $('recKey')) $('recKey').value = st.recording.key || '';
    }
    renderSteps(st.recording.steps);
    $('btnRecSave').disabled = st.recording.count === 0;
  } else {
    recSig = '';
  }
}

// ---------------------------------------------------------------- 录制
function renderSteps(steps) {
  const ol = $('stepList');
  ol.textContent = '';
  $('stepCount').textContent = steps.length;
  steps.forEach((label, i) => {
    const li = document.createElement('li');
    li.className = 'step-item';
    const idx = document.createElement('span');
    idx.className = 'idx';
    idx.textContent = String(i + 1).padStart(2, '0');
    const txt = document.createElement('span');
    txt.className = 'txt' + (label.startsWith('点击') ? '' : ' cmd');
    txt.textContent = label;
    li.append(idx, txt);
    ol.append(li);
  });
  ol.scrollTop = ol.scrollHeight;
}

async function startRecording() {
  // key 让服务端分配：以前用 S.tasks.length+1，删过任务/连续录制时列表没刷新
  // 回来，两次会算出同一个 key，save 直接覆盖同名 yaml —— 新录的把刚录的顶掉。
  const r = await api('/api/rec/start', { key: `task_${S.tasks.length + 1}`, name: '' });
  if (!r.ok) return toast(r.hint || '无法开始录制', true);
  const key = r.key;   // 服务端保证唯一，回填到「标识」框
  // 服务端内部会把空名字回退成 key（方便日志），但输入框要留给用户填，
  // 所以这里先标记 recSig 已初始化，让 applyState 别回填那个默认值。
  recSig = key;
  $('recName').value = '';
  $('recKey').value = key;
  $('recName').focus();
  refreshState();
  toast('开始录制：点画面记录步骤');
}

async function saveRecording() {
  const name = $('recName').value.trim();
  if (!name) { $('recName').focus(); return toast('先填任务名', true); }
  const key = $('recKey').value.trim() || undefined;
  const r = await api('/api/rec/save', { key, name });
  if (!r.ok) return toast(r.hint || '保存失败', true);
  toast(`已保存「${name}」`);
  // 用服务端最终落盘的 key（若手改的标识撞车，服务端会换一个空闲 key），
  // 否则选中的是旧 key，新录的任务看起来没被勾上。
  S.selected.add(r.key || key || name);
  saveSelected();
  refreshState();
}

// ---------------------------------------------------------------- 画面点击
img.addEventListener('click', async (e) => {
  if (S.busy) return;
  if (!S.connected) return toast('还没连上游戏画面', true);
  if (S.running) return toast('任务执行中，先停止再操作', true);

  const rect = img.getBoundingClientRect();
  const nw = img.naturalWidth || 1;
  const nh = img.naturalHeight || 1;
  const scale = Math.min(rect.width / nw, rect.height / nh);
  const dw = nw * scale;
  const dh = nh * scale;
  const ox = rect.left + (rect.width - dw) / 2;
  const oy = rect.top + (rect.height - dh) / 2;

  const nx = (e.clientX - ox) / dw;
  const ny = (e.clientY - oy) / dh;
  if (nx < 0 || nx > 1 || ny < 0 || ny > 1) return;

  S.busy = true;
  try {
    if (S.recording) {
      // fid 传 null：让服务端现抓一张 2560×1440 原图来裁模板。
      // 预览流是 1920×1080 的，拿它裁出来的模板会糊。
      const r = await api('/api/rec/tap', { x: nx, y: ny, fid: null });
      if (!r.ok) toast(r.hint || '这一下没记上', true);
      else if (r.warning) toast(r.warning, true);
      refreshState();
    } else {
      const r = await api('/api/tap', { x: nx, y: ny });
      if (!r.ok) toast(r.hint || '点击失败', true);
    }
  } catch (err) {
    toast('操作失败：' + err.message, true);
  } finally {
    S.busy = false;
  }
});

// ---------------------------------------------------------------- 事件绑定
$('btnRun').onclick = async () => {
  const keys = [...S.selected];
  if (!keys.length) return toast('先勾选要执行的任务', true);
  const r = await api('/api/run', { keys });
  if (!r.ok) return toast(r.hint || '启动失败', true);
  toast(`开始执行 ${r.count} 个任务`);
  logBox.scrollTop = logBox.scrollHeight;
  refreshState();
};

$('btnStop').onclick = async () => {
  // 必须传 body 让 api() 走 POST —— /api/stop 只在 do_POST 里处理。
  // 以前漏传 body 发成了 GET，后端 404，请求静默失败，任务根本收不到停止指令。
  try {
    const r = await api('/api/stop', {});
    if (!r.ok) toast(r.hint || '', true);
    else toast('正在停止…');
  } catch (e) {
    toast('停止请求失败：' + (e && e.message ? e.message : e), true);
  }
};

$('btnShot').onclick = async () => {
  const r = await api('/api/shot', {});
  if (r.ok) toast('截图已保存到 logs/shots');
  else toast('截图失败', true);
};

// 画面预览开关。默认关着更省资源（游戏本来就在旁边窗口里），
// 但录制必须在画面上点，所以录制时会自动打开。
$('btnPreview').onclick = async () => {
  const on = !S.preview;
  try {
    const r = await api('/api/preview', { on });
    S.preview = r.preview;
    if (on) startStream(); else stopStream();
    refreshState();
  } catch (e) {
    toast('切换失败：' + e.message, true);
  }
};

// 帧率模式：点一下循环 自动 → 60 → 省电。自动跟随预览开关（开=60/关=20）。
$('btnFps').onclick = async () => {
  const cur = (S.capture && S.capture.fps_mode) || 'auto';
  const next = { auto: 'high', high: 'low', low: 'auto' }[cur] || 'auto';
  try {
    await api('/api/fps/mode', { mode: next });
    await refreshState();
    toast('帧率：' + { auto: '自动（跟随画面开关）', high: '60 fps', low: '省电' }[next]);
  } catch (e) {
    toast('切换失败：' + e.message, true);
  }
};

$('btnReload').onclick = async () => {
  const r = await api('/api/reload', {});
  toast(r.ok ? `已刷新，共 ${r.count} 个任务` : '刷新失败', !r.ok);
  refreshState();
};

// 重新对齐：重新把画面铺满窗口并居中。
// 加载完会自动做一次；没赶上（加载慢/中途手动点过）就点这个再来一次。
$('btnRealign').onclick = async () => {
  const b = $('btnRealign');
  b.disabled = true;
  b.textContent = '对齐中…';
  try {
    const r = await api('/api/game/realign', {});
    if (r.ok) toast(`画面已重新对齐（舞台 ${r.stage[0]}×${r.stage[1]} ×${r.scale}）`);
    else toast(r.hint || '对齐失败，看日志', true, 7000);
  } catch (e) {
    toast('对齐失败：' + e.message, true);
  } finally {
    b.disabled = false;
    b.textContent = '重新对齐';
    refreshState();
  }
};

// 还原画面（黑屏急救）：撤掉位移/缩放，回到 1:1 居中。
// 对齐算歪、或者上次运行留下坏位移把游戏顶出窗口（整块黑屏）时点它 —— 不刷新、
// 不重登，画面立刻回来。
$('btnReset').onclick = async () => {
  const b = $('btnReset');
  b.disabled = true;
  b.textContent = '还原中…';
  try {
    const r = await api('/api/game/reset', {});
    if (r.ok) toast(`画面已还原（舞台 ${r.stage[0]}×${r.stage[1]} 1:1 居中）`);
    else toast(r.hint || '还原失败，看日志', true, 7000);
  } catch (e) {
    toast('还原失败：' + e.message, true);
  } finally {
    b.disabled = false;
    b.textContent = '还原画面';
    refreshState();
  }
};

// 缩放 100%：系统级——真的发 Ctrl+= 再 Ctrl+0（浏览器缩放 CDP 够不着）。
// 连接后默认会自动执行一次（可在配置里关）；这个是手动补做的入口。
// 会把游戏窗口抢到前台 1~2 秒。缩放完顺带重新对齐。
$('btnZoom').onclick = async () => {
  const b = $('btnZoom');
  b.disabled = true;
  b.textContent = '缩放中…';
  try {
    const r = await api('/api/game/zoom', {});
    if (r.ok) {
      const vb = (r.before || []).slice(0, 2).join('×') || '?';
      const va = (r.after || []).slice(0, 2).join('×') || '?';
      toast(`网页缩放已拨到 100%（视口 ${vb} → ${va}）`);
    } else {
      toast(r.hint || '缩放没成功，看日志', true, 7000);
    }
  } catch (e) {
    toast('缩放失败：' + e.message, true);
  } finally {
    b.disabled = false;
    b.textContent = '缩放 100%';
    refreshState();
  }
};

// 只重进游戏框：登录报错（如 1002）、画面卡住时用它，站点会话不受影响。
$('btnGameReload').onclick = async () => {
  const b = $('btnGameReload');
  b.disabled = true;
  b.textContent = '重载中…';
  try {
    const r = await api('/api/game/reload', { cache: true });
    if (r.ok && r.connected) toast('游戏画面已重载');
    else toast(r.hint || '重载后还没准备好，看日志', true);
  } catch (e) {
    toast('重载失败：' + e.message, true);
  } finally {
    b.disabled = false;
    b.textContent = '重载游戏';
    refreshState();
  }
};

// 粘贴 erolabsjwt 直接登录：登录页的人机验证在自动化环境下常过不去，这条路最稳
$('btnLogin').onclick = async () => {
  const v = window.prompt(
    '粘贴 erolabsjwt 的值\n\n'
    + '在平时用的浏览器里打开 ero-labs.com 并登录，然后：\n'
    + 'F12 → Application（应用）→ Cookies → https://www.ero-labs.com\n'
    + '找到 erolabsjwt，复制它的 Value 粘到这里。'
  );
  if (!v || !v.trim()) return;
  const b = $('btnLogin');
  b.disabled = true;
  b.textContent = '登录中…';
  try {
    const r = await api('/api/login', { jwt: v.trim() });
    toast(r.connected ? '登录成功，已接上游戏' : (r.hint || '还是没连上，看日志'), !r.connected);
  } catch (e) {
    toast('登录失败：' + e.message, true);
  } finally {
    b.disabled = false;
    b.textContent = '登录令牌';
    refreshState();
  }
};

// 借用登录态：把「你平时那个 Chrome」的 cookie 拷进工具自己的 profile。
// 为什么不能直接操作你那个窗口：CDP 只认带调试端口启动的浏览器，日常 Chrome
// 没开端口，而且 Chrome 136 起禁止在默认 profile 上开端口。那就把登录态搬过来。
// 搬之前它得先松手：Chrome 开着时对 cookie 库是独占的，连只读都不行 ——
// 所以第一次点很可能先弹「要不要帮你关掉 Chrome」，点了才继续。
async function borrowLogin(close) {
  const b = $('btnBorrow');
  b.disabled = true;
  b.textContent = close ? '关浏览器…' : '导入中…';
  try {
    const r = await api('/api/login/import', { close: !!close });
    if (r.ok && r.connected) {
      toast(`已借用登录态（cookie ${r.cookies} 条，其中 ero-labs ${r.cookies_game} 条）`, false, 5000);
    } else if (r.ok) {
      toast(r.hint || '导入完成，但画面还没准备好', true, 7000);
    } else if (r.need_close && !close) {
      const name = r.browser || 'Chrome';
      if (confirm(`${name} 现在开着，它独占 cookie 库，拷不出来。\n\n`
        + `要我先关掉 ${name} 吗？会话会被保存，下次打开能恢复标签页。\n`
        + `（不想关就先自己退出 ${name}，再点一次「借用登录态」）`)) {
        return borrowLogin(true);          // 同意了 → 关掉再来一遍
      }
      toast(`那就先自己退出 ${name}，再点一次「借用登录态」`, true, 6000);
    } else {
      toast(r.hint || '导入失败', true, 7000);
    }
  } catch (e) {
    toast('导入失败：' + e.message, true);
  } finally {
    b.disabled = false;
    b.textContent = '借用登录态';
    refreshState();
  }
}

$('btnBorrow').onclick = () => {
  if (!confirm('把本机 Chrome（或 Edge）的登录 cookie 复制到工具自己的浏览器？\n\n'
    + '· 只读你平时的 profile；工具原来的登录态会备份到 chrome-data/.import-backup/\n'
    + '· 会先关掉工具的那个浏览器窗口（导入完自动重开）\n'
    + '· 重开之后工具里就是已登录状态，不用再登一次')) return;
  borrowLogin(false);
};

$('btnAll').onclick = () => {
  if (!S.tasks.length) {
    toast('任务清单还没加载出来，稍等一下', true);
    return refreshState();
  }
  // 只勾「模板齐全」的：缺模板的任务跑起来必定失败，别让人白等
  const all = S.tasks.filter((t) => !(t.missing && t.missing.length));
  const everySelected = all.every((t) => S.selected.has(t.key));
  S.selected = everySelected ? new Set() : new Set(all.map((t) => t.key));
  saveSelected();
  S.rendered = '';   // 强制重绘
  toast(everySelected ? '已全部取消' : `已勾选 ${all.length} 个任务`);
  refreshState();
};

$('btnOrder').onclick = openOrderDlg;
$('orderClose').onclick = closeOrderDlg;
$('orderCancel').onclick = closeOrderDlg;
$('orderSave').onclick = saveOrderText;
$('orderDlg').onclick = (e) => {
  if (e.target === $('orderDlg')) closeOrderDlg();   // 点黑底关掉
};
$('orderReveal').onclick = async () => {
  try {
    const r = await api('/api/tasks/order/reveal', {});
    if (!r.ok) toast(r.hint || '打不开文件夹', true);
  } catch (e) {
    toast('打不开文件夹：' + e.message, true);
  }
};

$('btnNewTask').onclick = startRecording;

$('btnRecSave').onclick = saveRecording;

$('btnRecQuit').onclick = async () => {
  if (!confirm('放弃这次录制？已录的模板会被删除。')) return;
  await api('/api/rec/cancel', { cleanup: true });
  refreshState();
};

$('btnUndo').onclick = async () => {
  const r = await api('/api/rec/undo', {});
  if (!r.ok) return toast(r.hint || '', true);
  refreshState();
};

for (const chip of document.querySelectorAll('.chip')) {
  chip.onclick = async () => {
    const op = chip.dataset.op;
    let value = null;
    if (op === 'wait_stable') value = 6;
    if (op === 'sleep') value = 1;
    if (op === 'scroll') value = 'down';
    const r = await api('/api/rec/cmd', { op, value });
    if (!r.ok) toast(r.hint || '', true);
    refreshState();
  };
}

document.addEventListener('keydown', (e) => {
  if (!$('orderDlg').hidden) {
    if (e.key === 'Escape') { e.preventDefault(); return closeOrderDlg(); }
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      return saveOrderText();
    }
    return;      // 弹窗开着时别让下面的快捷键抢输入
  }
  if (!$('updDlg').hidden) {
    if (e.key === 'Escape') { e.preventDefault(); $('updDlg').hidden = true; }
    return;
  }
  if (e.key === 'Escape' && S.running) $('btnStop').click();
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) $('btnRun').click();
});

// 日志区高度可拖拽
(function () {
  const bar = $('logBar');
  let dragging = false;
  $('vresize').addEventListener('mousedown', (e) => {
    dragging = true;
    e.preventDefault();
    document.body.style.cursor = 'ns-resize';
  });
  window.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const h = window.innerHeight - e.clientY;
    bar.style.flexBasis = Math.max(70, Math.min(h, window.innerHeight - 280)) + 'px';
  });
  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    document.body.style.cursor = '';
  });
})();

// ---------------------------------------------------------------- 自动更新
// 版本号由服务端（core/update.py）从 GitHub Release 查来。有新版时顶栏
// 版本号会变成「v1.0.0 → v1.0.1」并高亮，点开弹窗就能一键更新：
// 程序自己退出 → update.ps1 换掉 _internal 和 exe → 重新拉起 → 面板自己刷新。
// 仓库私有或改名时服务端只回一句「暂不可访问」，这里照常显示当前版本。
let updWatching = false;

function fmtSize(n) {
  if (!n) return '';
  return n > 1024 * 1024 ? (n / 1024 / 1024).toFixed(0) + ' MB'
                        : Math.max(1, Math.round(n / 1024)) + ' KB';
}

function renderVersion(u) {
  const btn = $('btnVer');
  if (!u || !u.current) { btn.textContent = 'v—'; return; }
  if (u.stage === 'downloading') {
    btn.textContent = `更新中 ${u.progress || 0}%`;
    btn.classList.add('new');
    btn.title = '正在下载新版本';
    return;
  }
  btn.textContent = u.has_update ? `v${u.current} → v${u.latest}` : `v${u.current}`;
  btn.classList.toggle('new', !!u.has_update);
  btn.title = u.has_update ? `发现新版本 v${u.latest}，点击更新`
    : (u.ok ? '已是最新版本，点击重新检查' : (u.hint || '点击检查更新'));
}

function renderUpdate(u) {
  S.update = u || null;
  if (!u) return;
  renderVersion(u);
  if ($('updDlg').hidden) return;   // 弹窗没开就只更新顶栏，别动里面的文字

  const hint = $('updHint'), notes = $('updNotes'), prog = $('updProg'),
        bar = $('updBar'), ptext = $('updProgText'),
        apply = $('updApply'), page = $('updPage');
  const stage = u.stage || 'idle';

  // 失败后还能再试一次，所以 failed 也算「可以点」
  const canPress = stage === 'idle' || stage === 'failed';
  apply.hidden = !(u.can_apply && u.has_update && canPress);
  apply.disabled = !canPress;
  apply.textContent = stage === 'failed' ? '重试更新' : '立即更新';
  page.hidden = !u.url;

  const busy = stage === 'downloading' || stage === 'ready' || stage === 'restarting';
  prog.hidden = !busy;
  if (busy) {
    const pct = stage === 'downloading' ? (u.progress || 0) : 100;
    bar.style.width = pct + '%';
    ptext.textContent = stage === 'downloading'
      ? `${pct}%  ${fmtSize(u.got || 0)} / ${fmtSize(u.total || 0)}`
      : (stage === 'ready' ? '下载完成，准备重启…' : '正在重启…');
  }

  if (stage === 'downloading') {
    hint.textContent = `正在下载 v${u.latest}，下完会自动关闭并重启（约 10 秒），先别关窗口。`;
  } else if (busy) {
    hint.textContent = '正在换包重启，面板会自己刷新，稍等十来秒…';
  } else if (stage === 'failed') {
    hint.textContent = '更新失败：' + (u.err || '未知原因') + '（旧版本还能用，可以稍后重试）';
  } else if (u.has_update) {
    hint.textContent = `当前 v${u.current}，最新 v${u.latest}`
      + (u.published ? `（${u.published.slice(0, 10)} 发布）` : '')
      + (u.frozen ? '。点「立即更新」会自动下载并重启，不会动配置、模板和登录态。'
                  : '。');
    if (u.hint) hint.textContent += ' ' + u.hint;
  } else if (u.ok) {
    hint.textContent = `已是最新版本 v${u.current}`
      + (u.checked_at ? `（${new Date(u.checked_at * 1000).toLocaleTimeString()} 检查）` : '');
  } else {
    hint.textContent = u.hint || '检查失败，稍后再试';
  }

  notes.hidden = !u.notes;
  if (u.notes) notes.textContent = u.notes;
}

async function checkUpdate(manual) {
  const btn = $('updCheck');
  btn.disabled = true;
  $('updHint').textContent = '正在检查 GitHub…';
  try {
    const r = await api('/api/update/check', {});
    const u = r.update || {};
    renderUpdate(u);
    if (manual) {
      toast(u.has_update ? `发现新版本 v${u.latest}` : '已是最新版本', false, 3200);
    }
  } catch (e) {
    $('updHint').textContent = '检查失败：' + e.message + '（连不上的话不影响使用）';
  } finally {
    btn.disabled = false;
  }
}

function openUpdDlg() {
  $('updDlg').hidden = false;
  renderUpdate(S.update || {});
  if (!S.update || !S.update.checked_at || !S.update.ok) checkUpdate(false);
}

async function applyUpdate() {
  if (!confirm('更新会先关闭当前程序，下载完成后自动重启。现在开始？')) return;
  const cur = (S.update && S.update.current) || '';
  let r;
  try {
    r = await api('/api/update/apply', {});
  } catch (e) {
    r = { ok: false, hint: e.message };
  }
  if (!r.ok) return toast(r.hint || '启动更新失败', true);
  $('updApply').disabled = true;
  watchUpdate(cur);
}

// 盯着状态直到「进程退出 → 新版本起来」，版本号变了就刷新面板。
async function watchUpdate(oldVer) {
  if (updWatching) return;
  updWatching = true;
  let restarted = false;
  for (let i = 0; i < 300; i++) {
    await sleep(700);
    let st = null;
    try { st = await api('/api/state'); } catch (e) { st = null; }
    if (st && st.update) {
      renderUpdate(st.update);
      if (st.update.stage === 'failed') {
        toast('更新失败：' + (st.update.err || ''), true, 9000);
        updWatching = false;
        return;
      }
      if (restarted && st.update.stage === 'restarting') restarted = true;
      if (oldVer && st.version && st.version !== oldVer) {
        toast('已更新到 v' + st.version + '，正在刷新面板');
        await sleep(700);
        location.reload();
        return;
      }
      if (restarted && st.update.stage === 'idle') {
        // 起来了但还是老版本：换包失败自动回滚了
        toast('更新没生效（已回滚到 v' + st.version + '），可以稍后重试', true, 9000);
        updWatching = false;
        return;
      }
    } else if (restarted && !$('updDlg').hidden) {
      $('updHint').textContent = '程序已退出，正在等新版本起来…';
    }
  }
  updWatching = false;
  toast('没等到新版本响应，手动重开 KyHelper_Web.exe 即可', true, 8000);
}

$('btnVer').onclick = openUpdDlg;
$('updClose').onclick = () => { $('updDlg').hidden = true; };
$('updDlg').onclick = (e) => { if (e.target === $('updDlg')) $('updDlg').hidden = true; };
$('updCheck').onclick = () => checkUpdate(true);
$('updApply').onclick = applyUpdate;
$('updPage').onclick = () => {
  const u = S.update || {};
  if (u.url) window.open(u.url, '_blank');
};

// ---------------------------------------------------------------- 启动
// ?once=1 只取一次画面，用于截图和静态预览（持续轮询会让无头浏览器永远不空闲）
const ONCE = new URLSearchParams(location.search).has('once');

loadSelected();
api('/api/state').then(applyState).then(() => {
  if (!S.tasks.length) toast('还没有任务，点左下角开始录制');
}).catch(() => toast('连不上本地服务', true));

if (ONCE) {
  setTimeout(fetchOnce, 100);
  api('/api/log?since=0').then((d) => {
    if (d.records) appendLog(d.records);
    S.logSeq = d.seq;
  }).catch(() => {});
} else {
  // 不在这里开画面流：等第一次 /api/state 回来看 preview 开关
  logLoop();
  stateLoop();
}
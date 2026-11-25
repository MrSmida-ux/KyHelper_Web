/* 日常助手 · 游戏窗口内的浮层
 *
 * 为什么是浮层：游戏必须由一个**可见的 Chrome 窗口**真实渲染（机器人靠
 * Page.startScreencast 取帧，窗口一旦被遮住/最小化/挪到后台，Unity 就停止出帧），
 * 所以那个游戏窗口删不掉。要「一个窗口」，只能反过来把助手搬进游戏窗口 ——
 * 就是这个文件干的事。它由 CDP 的 Page.addScriptToEvaluateOnNewDocument 直接
 * 执行（不是 <script>，不受游戏站 CSP 限制），用 Shadow DOM 隔离样式。
 *
 * 两条硬约束：
 *   1) 机器人按**绝对坐标**点击，浮层只要接收点击，就可能把该点游戏的那一下吃掉。
 *      所以跑任务时整块浮层 pointer-events:none，并缩成右上角一小条。
 *   2) 跑任务时不能消失（否则看不到进度），但要能停下来 —— 用快捷键 Ctrl+Shift+X。
 *
 * 快捷键：Ctrl+Shift+X 停止 · Ctrl+Shift+H 展开/收起面板
 */
(function () {
  var API = "__KY_API__";
  var HOST_ID = "kyov";

  // 「注入过」不等于「现在还活着」：云游戏站点在加载过程中会换一次 DOM
  // （老式 document.write、或者框架把节点整个重挂），我们挂在 <html> 下的
  // 那个 div 会被一起清掉。所以每次执行都验一遍「还挂在文档里吗」，
  // 不在了就重建 —— 以前这里只看 window.__kyov_ok，被清掉一次就再也不回来了，
  // 用户看到的就是「启动时闪一下，然后就没了」。
  var alive = window.__kyov;
  if (alive && alive.isConnected && alive.shadowRoot) return;
  // 残骸清掉。它的守活定时器下面会自己退场（发现 window.__kyov 换人了就不再挂回来），
  // 不然两边各挂一份，页面上会冒出两个浮层。
  if (alive) { try { alive.remove(); } catch (e) {} }
  var old = document.getElementById(HOST_ID);
  if (old && old !== alive) { try { old.remove(); } catch (e) {} }

  var st = null;           // 最近一次 /api/state
  var logSeq = 0;          // 已经拉到的日志序号
  var sel = [];            // 本地勾选的任务 key
  var expanded = true;     // 面板是否展开（跑起来会自动收起）
  var wasRunning = false;
  var startedAt = 0;
  var offline = false;

  // ------------------------------------------------------------ 骨架
  var host = document.createElement("div");
  host.id = HOST_ID;
  host.__kyov = true;
  // 外层不吃点击，只有卡片自己吃（这样空出来的区域照样能操作游戏）
  host.style.cssText = "position:fixed;top:12px;right:12px;z-index:2147483600;pointer-events:none;";

  // 挂在 <html> 下而不是 <body> 里：站点换 body 内容时不会连累我们。
  // 但脚本可能在 <html> 还没建出来时就执行（新文档注入真的很早），所以要等。
  function mount() {
    var root = document.documentElement || document.body;
    if (!root) { setTimeout(mount, 30); return; }
    root.appendChild(host);
  }
  window.__kyov = host;        // 先占位：上一份浮层的守活器看到换人就退场
  mount();

  var shadow = host.attachShadow({ mode: "open" });
  shadow.innerHTML = [
    "<style>",
    ":host{all:initial}",
    "*{box-sizing:border-box;font-family:'Microsoft YaHei',-apple-system,Segoe UI,sans-serif}",
    ".card{pointer-events:auto;width:352px;background:rgba(16,19,26,.95);color:#e7eaf1;",
    "  border:1px solid rgba(255,255,255,.10);border-radius:12px;overflow:hidden;",
    "  box-shadow:0 10px 30px rgba(0,0,0,.45);font-size:12px}",
    ".hd{display:flex;align-items:center;gap:7px;padding:9px 10px;background:rgba(255,255,255,.04);",
    "  border-bottom:1px solid rgba(255,255,255,.07)}",
    ".dot{width:8px;height:8px;border-radius:50%;background:#8a93a6;flex:0 0 8px}",
    ".dot.ok{background:#35d07f;box-shadow:0 0 6px #35d07f88}",
    ".dot.bad{background:#ff6b6b;box-shadow:0 0 6px #ff6b6b88}",
    ".title{font-weight:600;font-size:12.5px;letter-spacing:.3px}",
    ".sub{color:#8a93a6;font-size:11px;margin-left:auto;white-space:nowrap;overflow:hidden;",
    "  text-overflow:ellipsis;max-width:180px}",
    ".mini{pointer-events:auto;background:transparent;border:0;color:#8a93a6;cursor:pointer;",
    "  font-size:14px;line-height:1;padding:0 2px}",
    ".mini:hover{color:#e7eaf1}",
    ".body{padding:8px 10px 4px}",
    ".line{padding:2px 0;color:#c3c9d6;font-size:11.5px}",
    ".line b{color:#e7eaf1;font-weight:600}",
    ".alert{color:#ffcc66}",
    ".tasks{max-height:132px;overflow:auto;margin:6px 0 4px;padding:4px;border-radius:8px;",
    "  background:rgba(255,255,255,.03)}",
    ".t{display:flex;align-items:center;gap:7px;padding:3px 4px;border-radius:5px;cursor:pointer;",
    "  overflow:hidden}",
    ".t:hover{background:rgba(255,255,255,.05)}",
    ".t input{appearance:none;-webkit-appearance:none;flex:0 0 12px;width:12px;height:12px;margin:0;",
    "  border:1px solid rgba(255,255,255,.28);border-radius:3px;background:transparent;",
    "  cursor:pointer;position:relative}",
    ".t input:checked{background:#3b82f6;border-color:#3b82f6}",
    ".t input:checked::after{content:'';position:absolute;left:3px;top:1px;width:4px;height:7px;",
    "  border:solid #fff;border-width:0 1.5px 1.5px 0;transform:rotate(42deg)}",
    ".t span.n{flex:1 1 auto;min-width:0;white-space:nowrap;overflow:hidden;",
    "  text-overflow:ellipsis;font-size:11.5px}",
    ".t span.s{flex:0 0 auto;font-size:10px;color:#8a93a6;margin-left:auto}",
    ".t span.s.ok{color:#35d07f}.t span.s.bad{color:#ff6b6b}.t span.s.warn{color:#ffcc66}",
    ".tasks::-webkit-scrollbar,.log::-webkit-scrollbar{width:6px}",
    ".tasks::-webkit-scrollbar-thumb,.log::-webkit-scrollbar-thumb{",
    "  background:rgba(255,255,255,.16);border-radius:3px}",
    ".t span.s.ok{color:#35d07f}.t span.s.bad{color:#ff6b6b}",
    ".log{height:118px;overflow:auto;background:#0b0e13;border:1px solid rgba(255,255,255,.06);",
    "  border-radius:8px;padding:6px;font-family:Consolas,monospace;font-size:10.5px;",
    "  line-height:1.5;color:#9aa3b2;white-space:pre-wrap;word-break:break-all}",
    ".log .w{color:#ffcc66}.log .e{color:#ff6b6b}",
    ".ft{display:flex;gap:6px;padding:8px 10px 10px;flex-wrap:wrap}",
    ".btn{pointer-events:auto;background:rgba(255,255,255,.07);color:#dfe4ee;border:1px solid rgba(255,255,255,.10);",
    "  border-radius:7px;padding:5px 9px;font-size:11.5px;cursor:pointer}",
    ".btn:hover{background:rgba(255,255,255,.12)}",
    ".btn.primary{background:#2f6fed;border-color:#2f6fed;color:#fff}",
    ".btn.primary:hover{background:#3b82f6}",
    ".btn:disabled{opacity:.45;cursor:not-allowed}",
    ".pill{pointer-events:none;display:flex;align-items:center;gap:7px;padding:5px 11px;",
    "  background:rgba(16,19,26,.88);border:1px solid rgba(255,255,255,.12);border-radius:999px;",
    "  color:#dfe4ee;font-size:11.5px;box-shadow:0 6px 18px rgba(0,0,0,.4)}",
    ".spin{width:7px;height:7px;border-radius:50%;background:#3b82f6;animation:p 1s infinite}",
    "@keyframes p{0%,100%{opacity:.3}50%{opacity:1}}",
    ".hint{color:#8a93a6;font-size:10.5px}",
    "</style>",
    '<div class="pill" id="pill" style="display:none">',
    '  <span class="spin"></span><span id="pillTxt">助手</span>',
    '  <span class="hint">Ctrl+Shift+X 停止</span>',
    "</div>",
    '<div class="card" id="card">',
    '  <div class="hd">',
    '    <span class="dot" id="dot"></span><span class="title">日常助手</span>',
    '    <span class="sub" id="sub">连接中…</span>',
    '    <button class="mini" id="fold" title="收起（Ctrl+Shift+H）">—</button>',
    "  </div>",
    '  <div class="body">',
    '    <div class="line" id="cur"></div>',
    '    <div class="line alert" id="alert" style="display:none"></div>',
    '    <div class="tasks" id="tasks"></div>',
    '    <div class="log" id="log"></div>',
    "  </div>",
    '  <div class="ft">',
    '    <button class="btn primary" id="run">▶ 执行勾选</button>',
    '    <button class="btn" id="stop">■ 停止</button>',
    '    <button class="btn" id="reset">还原画面</button>',
    '    <button class="btn" id="open">完整面板</button>',
    "  </div>",
    "</div>",
  ].join("\n");

  var $ = function (id) { return shadow.getElementById(id); };
  var card = $("card"), pill = $("pill"), pillTxt = $("pillTxt");
  var dot = $("dot"), sub = $("sub"), cur = $("cur"), alertBox = $("alert");
  var tasksBox = $("tasks"), logBox = $("log");
  var btnRun = $("run"), btnStop = $("stop"), btnReset = $("reset"), btnOpen = $("open");

  // ------------------------------------------------------------ 接口
  function api(path, body) {
    var opt = { cache: "no-store" };
    if (body !== undefined) {
      opt.method = "POST";
      opt.headers = { "Content-Type": "application/json" };
      opt.body = JSON.stringify(body);
    }
    return fetch(API + path, opt).then(function (r) { return r.json(); });
  }

  function post(path, body) {
    return api(path, body || {}).catch(function () { return { ok: false, hint: "连不上助手" }; });
  }

  // ------------------------------------------------------------ 渲染
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>]/g, function (c) {
      return c === "&" ? "&amp;" : c === "<" ? "&lt;" : "&gt;";
    });
  }

  function showExpanded(on) {
    expanded = on;
    card.style.display = on ? "" : "none";
    pill.style.display = on ? "none" : "flex";
    // 跑任务时小条绝不能吃点击（机器人按绝对坐标点，点到这里就白点了）
    pill.style.pointerEvents = wasRunning ? "none" : "auto";
  }

  // 服务端的状态值是英文的，界面上翻成中文，顺带决定颜色
  var STATUS_TXT = { ok: "成功", fail: "失败", skip: "跳过",
                     running: "进行中", pending: "待跑", error: "异常" };
  var STATUS_CLS = { ok: "ok", fail: "bad", error: "bad",
                     skip: "warn", running: "warn" };

  function renderTasks() {
    var list = (st && st.tasks) || [];
    var html = [];
    for (var i = 0; i < list.length; i++) {
      var t = list[i];
      var on = sel.indexOf(t.key) >= 0 ? " checked" : "";
      var s = (st.status && st.status[t.key]) || "";
      var cls = STATUS_CLS[s] || "";
      html.push(
        '<label class="t" data-key="' + esc(t.key) + '"><input type="checkbox"' + on + '>',
        '<span class="n">' + esc(t.name) + "</span>",
        '<span class="s ' + cls + '">' + esc(STATUS_TXT[s] || "") + "</span></label>"
      );
    }
    tasksBox.innerHTML = html.join("");
  }

  function appendLog(recs) {
    if (!recs || !recs.length) return;
    var atBottom = logBox.scrollTop + logBox.clientHeight >= logBox.scrollHeight - 8;
    for (var i = 0; i < recs.length; i++) {
      var r = recs[i];
      logSeq = Math.max(logSeq, r.seq || 0);
      var d = document.createElement("div");
      if (r.level === "WARNING") d.className = "w";
      else if (r.level === "ERROR") d.className = "e";
      d.textContent = (r.t || "") + "  " + (r.msg || "");
      logBox.appendChild(d);
    }
    while (logBox.childNodes.length > 400) logBox.removeChild(logBox.firstChild);
    if (atBottom) logBox.scrollTop = logBox.scrollHeight;
  }

  function render() {
    if (!st) return;
    // 顶栏
    var running = !!st.running;
    dot.className = "dot " + (st.connected ? (running ? "ok" : "ok") : "bad");
    var bits = [];
    if (!st.connected) bits.push("未连接");
    else bits.push(running ? "运行中" : "空闲");
    if (st.capture && st.capture.target_fps) bits.push(Math.round(st.capture.fps || 0) + "fps");
    sub.textContent = bits.join(" · ");

    // 当前任务 / 汇总
    var curName = "";
    var list = st.tasks || [];
    for (var i = 0; i < list.length; i++) if (list[i].key === st.current) curName = list[i].name;
    var parts = [];
    if (running) parts.push("<b>" + esc(curName || "准备中") + "</b>");
    if (st.last_summary) parts.push(esc(st.last_summary));
    cur.innerHTML = parts.join("　") || '<span style="color:#8a93a6">就绪</span>';

    // 提醒（失败原因等）
    var am = (st.alert && st.alert.msg) || st.hint || "";
    alertBox.style.display = am ? "" : "none";
    alertBox.textContent = am;

    // 跑起来 / 停下来 时切换形态
    if (running && !wasRunning) { startedAt = Date.now(); showExpanded(false); }
    if (!running && wasRunning) { showExpanded(true); }
    wasRunning = running;
    pill.style.pointerEvents = running ? "none" : "auto";

    btnRun.disabled = running || !st.connected;
    btnStop.disabled = !running;
    if (!running) pillTxt.textContent = "助手 · 空闲";

    renderTasks();
  }

  function tickPill() {
    if (!wasRunning) return;
    var s = Math.round((Date.now() - startedAt) / 1000);
    var name = "";
    if (st) {
      var list = st.tasks || [];
      for (var i = 0; i < list.length; i++) if (list[i].key === st.current) name = list[i].name;
    }
    pillTxt.textContent = "助手运行中 " + s + "s" + (name ? " · " + name : "");
  }

  function refresh() {
    api("/api/state").then(function (s) {
      offline = false;
      window.__kyov_api_ok = true;
      st = s;
      if (!sel.length) sel = (s.selection || []).slice();
      render();
      if (s.log_seq > logSeq) {
        return api("/api/log?since=" + logSeq).then(function (d) { appendLog(d.records); });
      }
    }).catch(function () {
      window.__kyov_api_ok = false;
      if (!offline) {
        offline = true;
        dot.className = "dot bad";
        sub.textContent = "连不上助手";
        alertBox.style.display = "";
        alertBox.textContent = "连不上 " + API + "（助手是不是没开？）";
      }
    });
  }

  // ------------------------------------------------------------ 交互
  tasksBox.addEventListener("change", function (e) {
    var box = e.target;
    if (!box || box.type !== "checkbox") return;
    var row = box.closest(".t");
    if (!row) return;
    var key = row.getAttribute("data-key");
    var i = sel.indexOf(key);
    if (box.checked && i < 0) sel.push(key);
    if (!box.checked && i >= 0) sel.splice(i, 1);
    post("/api/tasks/select", { keys: sel }).then(refresh);
  });

  btnRun.addEventListener("click", function () {
    if (!sel.length) { alertBox.style.display = ""; alertBox.textContent = "先勾一个任务"; return; }
    btnRun.disabled = true;
    post("/api/run", { keys: sel }).then(function (r) {
      if (r && r.ok === false) {
        alertBox.style.display = "";
        alertBox.textContent = r.hint || "启动失败";
        btnRun.disabled = false;
      }
      setTimeout(refresh, 300);
    });
  });

  btnStop.addEventListener("click", function () { post("/api/stop").then(refresh); });
  btnReset.addEventListener("click", function () { post("/api/game/reset").then(refresh); });
  btnOpen.addEventListener("click", function () {
    var w = window.open(API + "/", "_blank", "width=1280,height=860");
    if (!w) { alertBox.style.display = ""; alertBox.textContent = "弹窗被拦了，手动开：" + API + "/"; }
  });
  $("fold").addEventListener("click", function () { showExpanded(false); });
  pill.addEventListener("click", function () { showExpanded(true); });

  window.addEventListener("keydown", function (e) {
    if (!e.ctrlKey || !e.shiftKey) return;
    var k = (e.key || "").toLowerCase();
    if (k === "x") { e.preventDefault(); post("/api/stop").then(refresh); }
    if (k === "h") { e.preventDefault(); showExpanded(!expanded); }
  }, true);

  showExpanded(true);
  window.__kyov = host;        // 服务端探活用（判断浮层是不是还在文档里）
  window.__kyov_ok = true;
  window.__kyov_api = API;     // 这一份浮层连的是哪个端口（换端口了要重注）
  window.__kyov_api_ok = null; // 接口通不通：null=还没试过，true/false=上次结果

  // 守活：页面把节点清掉就原地挂回去。只是重新 appendChild，
  // Shadow DOM 和里面的勾选/日志都还在，不会「闪一下重建」。
  // 判定条件里带 window.__kyov === host：自己已经被新浮层取代时绝不再挂回来。
  setInterval(function () {
    try {
      if (window.__kyov !== host) return;
      if (!host.isConnected) mount();
    } catch (e) {}
  }, 800);

  refresh();
  setInterval(refresh, 1500);
  setInterval(tickPill, 500);
})();

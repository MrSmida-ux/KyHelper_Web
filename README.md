# KyHelper_Web · 双穹幻想 Burst 网页版助手

驱动 ERO LABS 云游戏页（`cloud_game.html?id=55`）做日常的本地工具：Chrome + CDP 拿画面，
灰度模板匹配做识别，合成鼠标事件做点击，控制台是本地网页（不起 exe、不开投屏窗口）。

- 画面：`Page.startScreencast` 直接吃浏览器合成器的帧，没有请求往返
- 点击：`Input.dispatchMouseEvent`，落在游戏画布上，不抢鼠标、不抢前台
- 依赖：Chrome + opencv + numpy + PyYAML
- 任务：13 个内置 + 8 个录制，354 张模板
- 更新：免安装版自带版本检查，面板顶栏点版本号即可下载换包重启
- 登录：请看二-4节，作者本人使用的是方式3，开战前记得选好配队，或者把最强编队放在第1页第1个位置
  
> **不想装 Python？** 下 [**v1.2.0 免安装版**](https://github.com/MrSmida-ux/KyHelper_Web/releases/latest)
> （Windows x64，zip 约 71 MB）：解压后双击 `KyHelper_Web.exe` 就能跑，
> 运行时和 opencv/numpy 都打包在 `_internal/` 里，只需要本机装有 Chrome 或 Edge。
> 想改代码或自己加模板，再按下面的源码方式跑。

---

## 〇、使用前提（先看三条）

1. **只支持 E 服。** 所有模板和坐标都是照着 ERO LABS 那个云游戏页
   （`cloud_game.html?id=55`）录的。别的服（其他语言服 / 其他渠道服）界面文案、
   按钮位置、分辨率都不一样，识别会大面积对不上，**不要指望改改配置就能通用**。
2. **游戏语言必须是简体中文。** 354 张模板全程按简体中文界面录制，标题、按钮、
   弹窗文字都在模板里。切成 English / 日本語 之后，主页锚点都认不到，任务是跑不起来的。
3. **战斗配队请提前设好。** 脚本不负责配队、不换人、不调装备，只按模板点「战斗 /
   自动 / 开始」。队伍空着、人没上齐、战力不够，结果就是打不过或者卡在结算页。
   日常、迷宫、圣域这些要打的关卡，先在游戏里把队伍和自动战斗设置好再跑。

---

## 一、快速开始

```bash
pip install -r requirements.txt
python run.py            # 或双击 启动.cmd
```

启动后做四件事：

1. 起本地控制台 `http://127.0.0.1:8765/`（端口被占自动往后找）；
2. 拉起一份**独立的 Chrome**（profile 在 `chrome-data/`，不动你平时的浏览器），
   打开云游戏页，窗口默认最大化；
3. 收拾页面：隐藏站点导航/页脚、游戏 iframe 铺满、加载完对齐一次画面；
4. 第一次需要在那个窗口里**登录一次**（登录态存在 `chrome-data/`，以后不用再登）。

控制台左侧勾任务、拖序号排序、设属性/难度，右侧执行、看日志。游戏就在旁边的
Chrome 窗口里，**默认不投屏**（省 CPU）；要看点工具栏「画面 关」→「画面 开」，
录制任务时会自动打开。

命令行：

```bash
python run.py --probe           # 自检：浏览器/画面/识别耗时/帧率
python run.py --shot            # 存一张当前画面到 logs/shots
python run.py -l                # 列任务
python run.py -t mail -t ruins  # 只跑指定任务
python run.py -t ruins --dry    # 只识别不点击
```

---

## 二、画面与坐标

**基准坐标系只有一个：模板的 2560×1440。** 所有任务代码、模板 json、手工坐标都在
这个坐标系里。窗口怎么变，靠下面三道换算兜住。

### 1. 量画布（`core/browser.py: measure`）

游戏是 Unity WebGL，DOM 套三层：`页面 → iframe#game_frame → iframe#unityPlayer → canvas`，
画布本身被封顶在 **1280×720**。所以不能拿 iframe 的盒子当识别区域 —— 通过 OOPIF
调试会话（`Target.attachToTarget`）钻进游戏帧量**画布真实矩形**，加上各层偏移和
缩放比，得到它在父页面 CSS 坐标里的精确位置。

### 2. 加载后对齐一次（`settle_layout`）

认到**标题画面的 GAME 字样**（`title_logo.png`）就动手，这就是「加载完成」那一刻。
认不到就退回两条老信号：主页锚点出现，或画面静止两个周期（约 3 秒）。

| 步骤 | 做什么 | 为什么 |
|---|---|---|
| ① 舞台 = 画布尺寸 | 固定成量到的原生 1280×720 | 游戏按画布尺寸排版，内容不会被裁 |
| ② 撑满帧内层 | `#unityPlayer` 强成 `100vw × 100vh` | 站点把它设成 inner+40×inner+80，画布右下角会被裁 |
| ③ 顶到原点 | 量画布与舞台的差，差多少平移多少 | 游戏给画布留了 20px 边距 |
| ④ 放大 | `browser.fill` 打开时 `scale(k)`，`k = min(窗口/画布)`，**不超过窗口** | 去掉黑边 |
| ⑤ 居中 + 锁定 | 按偏差平移居中，`_settled = True` | 之后不再动，除非窗口变化/重载/手动对齐 |
| ⑥ 验收 | 量画布是否还在窗口里，不在就退回上一版排布 | 宁可小一点，不许黑屏 |

任何时候可以点 **「重新对齐」**；画面整块黑掉点 **「还原画面」**（只撤位移/缩放，
不刷新页面、不用重新登录）。对齐只动 DOM/CSS，不碰鼠标键盘。

几个容易踩的坑：

- **别清画布的 `margin`**。站点模板给画布留了 `margin: 20px 0 0 20px`，
  `#unityPlayer` 一等于视口，画布正好落在 `(0,0,1280,720)`；清了 margin 反而跑到
  `(-20,-20)`，裁掉右下角（就是「首页底部按钮显示不全」的成因）。
- 舞台样式只在 `prepare` 注一次会被**页面导航冲掉**，所以用
  `Page.addScriptToEvaluateOnNewDocument` 注入；但注入的只能是不带尺寸的基础样式，
  带尺寸会把注册那一刻的旧值（2560×1440）贴回去，舞台跑到屏幕外。
- 基础样式与舞台样式用**两个 `<style>`**（`__kyweb_css` / `__kyweb_stage`），
  否则互相覆盖；改尺寸后 Unity 会拆掉画布重建，那一两秒由 `_wait_canvas()` 等回来。
- **对齐不能并发**：任务结束时的 `refresh_geometry(force=True)` 与守护线程同时改 CSS
  会把位移累加出去（曾把游戏推出窗口导致全黑）。现在三道闸：`_settle_lock` 串行化、
  `_guard_shift` 削掉超过窗口 1/3 的位移、验收不过就回退。

### 3. 画面归一 + 点击换算

- `core/capture.py: _fit`：截到的是整个窗口（2x 屏约 2560×1345），按画布矩形裁掉
  黑边再缩到 2560×1440。2x 屏上画布本身就是 1280×720 CSS × 2，所以基本 1:1。
- `core/device.py: tap` → `GameSession.to_css`：
  `css = 画布原点 + 基准坐标 × (画布实际尺寸 / 2560×1440)`。

于是窗口拉大拉小、最大化、换显示器，任务一行都不用改；站点改布局，`measure`
每次都重量新矩形。设备像素比保持系统原样（不强制 1x），2x 屏相当于把高分辨率
降采样到模板尺寸，比 1:1 更清晰。

### 4. 填满窗口还是原生大小

| 配置 | 效果 |
|---|---|
| `browser.fill: true`（默认） | CSS 放大铺满窗口，黑边基本没有，画面略软 |
| `browser.fill: false` | 1280×720 原生大小居中，黑边长，一个像素不插值 |

### 5. 缩放 100%（手动按钮）

`Ctrl + 滚轮` / `Ctrl + =` 是**浏览器进程**的快捷键，CDP 合成事件够不着（发完
`innerWidth` 不变）。能生效的只有系统级注入（`core/winput.py` 用 `SendInput` 发
`Ctrl+=` / `Ctrl+0`），会抢前台 1~2 秒，所以做成**手动按钮**。想加载完自动做一次：
`browser.zoom_auto: true`。

---

## 三、登录（一次性）

四条路，任选：

1. **借用登录态**（最省事）：先在平时的 Chrome 里登录好游戏，控制台点「借用登录态」，
   把那份 profile 的 cookie 拷进 `chrome-data/`，重开即已登录。
2. **手动登**（默认）：在工具拉起的 Chrome 窗口里正常登录。
3. **粘 JWT**：F12 → Application → Cookies → `erolabsjwt`，把值填到 `config.yaml`
   的 `browser.jwt`，启动时自动种 cookie。
4. **重新登录**：掉登录时日志会提示「请在弹出的浏览器窗口里登录一次」。

### 为什么必须用独立 profile

两道硬门槛，绕不过去：

1. **CDP 只认带 `--remote-debugging-port` 启动的浏览器。** 你日常双击打开的那个
   没有端口，事后也加不上 —— 对别的程序来说它是黑盒。
2. **Chrome 136 起禁止在默认用户目录上开调试端口**（防止恶意程序读 cookie），
   所以「关了 Chrome 再用默认 profile 带端口启动」会被忽略，必须另给 `--user-data-dir`。

结论：**能控制的 profile 和你的日常 profile 天生不是一个**，只能搬登录态。

- **点「借用登录态」**（推荐）：拷 `Local State`（DPAPI 主密钥）+ `Default/Network/Cookies`
  + Preferences / Local Storage，`cf_clearance` 一起带过来，连带跳过人机验证。
  旧登录态先备份到 `chrome-data/.import-backup/<时间戳>/`。
  代价：Chrome 开着时 cookie 库独占（只读都 Permission denied），所以会问「要不要
  帮你关掉 Chrome」—— 关掉时会保存会话，下次能恢复标签页。
- **粘 JWT**：只搬一个值，不用关浏览器，但过期要再抄。
- **手动在那份 profile 里登一次**：不碰日常浏览器，代价是可能过不了 Turnstile。

### 登录页登不进去（錯誤代碼 1002 / 請稍後重新再試）

iframe 里显示的是**站点的会员登录页**（`signup.html?type=webgl`），不是游戏服登录。
站点脚本拿到 `erolabsjwt` 才会连云游戏，没登录就把账号页塞进 iframe。所以
「登不进去」= 站点会话没建立，而那里有一张 Cloudflare Turnstile，在自动化浏览器里
经常静默失败。

**别反复试**（会触发频控），按顺序：

1. 确认人机验证打了勾再点登录；错一次就停，等一两分钟。
2. 点工具栏「重载游戏」：清这份 profile 的 HTTP 缓存后重进账号页。
3. 最稳：点「借用登录态」，或点「登录令牌」粘 `erolabsjwt` 直接建会话，完全绕开
   登录页。取法（用平时的浏览器）：登录 `https://www.ero-labs.com` → F12 → Console
   → `document.cookie` → 复制 `erolabsjwt=` 后面那段（不是 httpOnly，能直接读）。
4. 也可以写进 `config.yaml` 的 `browser.jwt`，启动时自动注入。

控制台状态里带 `login_err`（如 `1002`），排错先看这个码。

---

## 四、性能

实测（2560×1440 视口）：

| 环节 | 数值 | 说明 |
|---|---|---|
| 取一帧画面 | 20~40ms | 合成器直接推帧，无请求往返 |
| 识别（局部 ROI） | 3.5~5ms | 绝大多数情况走这条 |
| 识别（全图兜底） | 15ms | 缩图粗搜 + 原图精修 |
| 画面解码 | 每 120ms 一次 | 按帧号+TTL 复用，一轮决策共用一帧 |
| 界面投屏 | 默认关闭 | 开也只做 1/2 分辨率解码 |

三招（都在 `core/`）：

1. **ROI 优先**（`vision.py`）：模板 json 记着当初裁切的位置，先在 ±180px 里找。
   局部 3.5ms vs 全图 52ms，快 15 倍且误匹配更少。
2. **粗搜 + 精修**（`_coarse_find`）：ROI 没命中才用，整图缩一半扫一遍再回原图精修，
   15ms vs 52ms，结果与全图一致。
3. **帧复用**（`capture.py`）：决策只有 3~4Hz，BGR/灰度图按帧号缓存 120ms；
   点完按钮会**等新帧到手**再识别，避免拿点击前的画面把同一步点两遍。

### 帧率

Chrome 合成器会按屏幕刷新全力推帧（实测 94fps × 2560×1440 JPEG，20~35MB/s），
而识别只要最新一帧。所以按档位控：

```yaml
capture:
  fps_high: 60   # 预览开/录制时：源端全速 + 接收端按时间戳限 60
  fps_low: 20    # 预览关时：源端隔帧（nth≈5），编码/传输一起省
  quality: 55    # JPEG 质量，62→55 再省约 15% 体积
ui:
  fps_mode: auto # auto=跟随预览开关 / high=锁60 / low=锁20
```

Chrome 只有 `everyNthFrame` 整数隔帧（94/1、/2=47、/3=31），凑不出 60，所以 high
档靠接收端按时间戳限帧。顶栏帧率按钮点一下循环 `自动 → 60 → 省电`，选择写回
`ui.fps_mode`。改 config 不用重启：`POST /api/capture/apply` 重开画面流即可。

### 运行期间去做别的事

窗口就老实摆在那儿（不挪走、不置顶、不抢焦点），遮挡不影响出帧。
唯一要防的是**最小化 / Win+D**：此时浏览器停止合成，帧率掉到 0，任务会卡住。
守护线程每 1.5 秒检查，发现最小化就原地恢复正常大小，帧率立刻回到 70fps+。
另外别把窗口缩到 320×200 那种尺寸，Chrome 会降级画面流。

### 浮层模式（已关闭）

默认 `ui.overlay: false` / `ui.panel: true`：游戏窗口 + 独立助手窗口。把面板注入
游戏窗口右上角的浮层实做出来了（`core/overlay.py` / `web/overlay.js`），但挡游戏
入口、可点区域小、还得记快捷键，所以关了。想试就改 `overlay: true` / `panel: false`。

> 浮层会吃点击，所以跑任务时它缩成一条小字且 `pointer-events: none`；
> `Ctrl+Shift+X` 停止，`Ctrl+Shift+H` 展开日志（展开后也不接收点击）。

---

## 五、没有返回键

浏览器里没有安卓 keycode 4，所以 `core/device.py: back()` 按顺序试三招：

1. 界面上有返回箭头模板（`btn_back` / `btn_back_relic` / `btn_story_back`）→ 点它；
2. 发 `Escape`（部分 WebGL 游戏认）；
3. 点 `game.back_point`（默认左上 `(70,70)`）。

**能点底部「首页」就别用返回键** —— `flows.ensure_home()` 本来就优先点 `tab_home`，
返回键只是兜底。某个页面的返回键点不动，就用录制把那个箭头录成 `btn_back` 放进
`assets/templates/`，`back()` 会自动优先用它。

---

## 六、任务

| 类型 | 任务 |
|---|---|
| 内置 13 | 缩放100%、体力领取、派遣领取、市场免费礼物、免费召唤、圣骑士送羁绊礼物、主线冒险、遗迹关卡、迷宫挑战、异策战、救援战·援助、救援战·发起、每日任务奖励领取 |
| 录制 8 | 游戏登录、同盟捐拜、竞技场3v3、竞技场1v1、资源收集、自动周回、领取PASS奖励、领取信件 |

任务带选项的在名字下面显示一排小药丸，单选（`single`）的没有「全部」，点了即替换；
选项间的显隐用 `show_when` 声明（例：迷宫选「跳过」时隐藏目标王/难度/加护/重刷上限）。

**迷宫挑战**是一个任务两种方式（游戏每天只给一次，所以合并）：
`跳过` = 走录制好的 7 步点 SKIP，十几秒；`手动` = 按指定王逐层逐房真打。
录制步骤放在 `tasks/recipes/_embedded/`，不会被当成独立任务列出来。

手动打模式的几个实现要点（踩过的坑）：

- **房间靠现扫**：每层的房间数量、位置、卡片大小都不一样（3~6 间，5 间那层比 6 间大约
  25%），所以不写死坐标，而是多尺度匹配每张卡顶部的**宝箱**定位房间，再用邻近的
  CLEAR 横幅 / 骷髅 / 小人图标判断状态。骷髅＝这间还没打，BOSS 通常就在里面。
- **重刷三扇门**：门的选择是整局随机一次，光取消弹窗不会变。第 1 层直接从左上返回
  退到冒险页再进；第 2 层起**返回上一层 → 点「前往下层」**，门就重新随机了。
- **换层要手动点**：一层全清后不会自动下去，得点「前往下层」；点完有加载过场，
  程序会等门页真的出现再动手（以前在过场里判断「不在门页」，直接退迷宫白扔一层）。
- **续打**：中途断了、或停在加护/结算页，再点任务会从当前层接着打，不回主页重走入口。
- `目标王` 选 `任意` = 第一扇门直接进、不重刷，最快通关。
- 加护页的「确定」按钮位置随弹窗行数变化（3 行比 4 行高 180px），一律用模板现找。

---

## 七、目录

```
run.py            入口（默认起控制台；--probe/--shot/-l/-t/--dry）
gui_server.py     本地控制台服务（127.0.0.1:8765）
config.yaml       全部可调项
core/
  cdp.py            极简 CDP 客户端（自己实现的 WebSocket，零第三方依赖）
  browser.py        Chrome 启停、页面收拾、画布测量、坐标换算
  chrome_profile.py 借用本机 Chrome 登录态
  capture.py        screencast 帧泵 + 解码缓存 + 预览小图
  vision.py         灰度模板匹配（ROI + 粗搜精修）
  device.py         任务看到的「设备」：tap/wait/wait_stable/back/scroll
  flows.py          回主页、关公告、关广告、取消 AP 购买框
  recipe.py         声明式步骤解释器（录制任务）
  recorder.py       录制：点一下自动裁模板
  log.py paths.py bootstrap.py
  update.py         查 GitHub Release、下载换包（面板顶栏的版本号）
tasks/            13 个内置任务 + recipes/（8 个录制 yaml）
assets/templates/ 354 张模板 + 同名 .json（记基准分辨率和裁切位置）
web/              控制台前端
chrome-data/      工具自己那份 Chrome 用户目录（登录态在这，不进 git）
logs/             运行日志、截图、失败现场（不进 git）
build.py          打包成免 Python 的 exe（PyInstaller）
release/          build.py 的产物（不进 git）
update/           自动更新下载的 zip / 解压目录 / 旧版备份（不进 git）
```

### 打包成 exe

```bash
python -m venv .venv-build
.venv-build\Scripts\pip install opencv-python==4.14.0.94 numpy PyYAML pyinstaller
python build.py --zip        # → release/KyHelper_Web/ 和 release/KyHelper_Web-<版本>-win64.zip
```

产物是**目录版**（`exe` + `_internal/` + 资源），不是单文件：单文件每次启动都要把
100 MB 运行时解到临时目录，首次要等 5~10 秒，目录版秒开，杀软误报也少。

打进去的资源和源码结构对应：`assets/templates`、`web`、`tasks/recipes`、`config.yaml`。
程序跑起来时 `core/paths.py` 把它们复制到 exe 同级目录一份（已存在的文件不覆盖，
用户改过的模板能留住）；内置的那份留在 `_internal/` 当兜底。

### 自动更新（`core/update.py`）

面板顶栏右侧显示当前版本（就是同目录 `VERSION` 的值）。程序启动后隔几个小时
去 GitHub 查一次最新 Release，有新版就变成 `v1.1.0 → v1.2.0` 并高亮，点它：

1. **下载**：挑 Release 里名字带 `win` 的 zip（附件改名也能认，退到任意 zip；
   只有源码包就不提供更新）。下载中面板显示百分比，日志在 `logs/update.log`；
2. **退出**：写一个 `update/update.ps1`，本进程收摊退出（Windows 上跑着的 exe 和
   `_internal/*.pyd` 自己删不掉自己）；
3. **换包**：脚本等进程消失后，把旧的 `KyHelper_Web.exe` 和 `_internal/` 挪进
   `update/backup/`，新版解压进来，然后带 `--updated` 重新拉起程序；
   换失败会自动回滚到旧版再启动；
4. **接管**：旧面板的页面在等处「版本号变了」，变了就 `location.reload()`；
   15 秒内没有任何请求（用户把窗口关了）才补开一个新窗口。

只换程序本体，**不碰** `config.yaml`、`assets/templates/`、`tasks/`、`logs/`、
`chrome-data/`（登录态）。新版新增的模板由 `ensure_layout` 在下次启动时补出来。

仓库私有或改名时 GitHub API 返回 404，这里只记一句「暂不可访问」然后跳过，
不影响任何功能；等仓库重新 public 下一次轮询自然就通了。fork 的人改
`update.repo` 即可。源码运行时不会提示自动更新（该 `git pull`）。

---

## 八、配置速查

```yaml
browser:
  port: 9222
  chrome: ""              # 留空自动找 Chrome/Edge
  window: "1440x860"      # 给你看的大小，不影响识别
  maximize: true
  fill: true              # 画布放大铺满窗口（false = 原生居中，黑边大）
  settle_timeout: 60      # 等主页锚点最多多久，超时先对齐
  zoom_auto: false        # 加载完自动拨到 100%（抢前台 1~2 秒，默认关）
  viewport: "2560x1440"   # 模板基准（不是视口，视口等于窗口）
  jwt: ""                 # 可选：直接种登录态

capture:
  fps: 30                 # 旧字段，被下面两档取代
  fps_high: 60
  fps_low: 20
  quality: 55

ui:
  port: 8765
  fps_mode: auto          # auto / high / low
  overlay: false          # 浮层模式（已关闭）
  panel: true             # 独立助手窗口
  preview: false          # 默认不投屏
  preview_width: 1024

template:
  threshold: 0.85         # 漏检降 0.80，误点升 0.90
  roi: true               # ROI 优先，建议一直开
  roi_margin: 180

input:
  tap_jitter: 3           # 点击随机偏移
  delay_min: 0.55         # 动作间随机间隔（秒）
  delay_max: 1.10

game:
  back_point: [70, 70]    # 返回键兜底坐标

update:
  check: true                       # 关掉就完全不查 GitHub
  repo: "MrSmida-ux/KyHelper_Web"   # fork / 改名后改这里
  interval_hours: 6                 # 多久查一次
```

---

## 九、排错

| 现象 | 处理 |
|---|---|
| 提示「请在弹出的浏览器窗口里登录一次」 | 没登录。在那个窗口登一次，或填 `browser.jwt` |
| 登录页 1002 /「請稍後重新再試」 | Turnstile 过不去。别反复试，直接「借用登录态」或粘 `erolabsjwt` |
| `--probe` 说调试端口没响应 | 9222 被占，改 `browser.port` |
| 帧率掉到个位数 / 画面冻住 | 窗口被最小化了。别最小化（遮挡没事），守护线程会自己恢复 |
| 点击点了没反应 | `--probe` 看「游戏区域」是不是 `0,0 1280×720`；不是说明站点改了布局 |
| 识别「超时」但画面上明明有 | 先把 `template.threshold` 降到 0.80；仍不行就录制重录那一步（模板按文件时间自动重载，不用重启） |
| 带字的按钮只有 0.7x 分 | **游戏语言变了**。模板基本按简体录的，切繁体后笔画错位（0.99→0.57），改回简体；部分模板备了 `_trad` 版本，`tap_any/exists_any` 会自己挑 |
| 扫荡次数不对 | 「+」只有上半部（y≈825~865）认，连点会被吞；并且**游戏会记住上次的数量**。`tasks/sweep.py` 每次先归 1 再设目标次数，日志出现「加到 N 次就点不动了」重跑一次即可 |
| 结算/报酬/召唤结果页关不掉，接着全任务失败 | 这些页晚到几秒，旧逻辑一眼没看到就放弃。现在 `ensure_home` 会主动按[关闭]位置关掉各类结果页，`close_results()` 会等（首屏 45s），并连续 4 轮稳定才算回列表 |
| 录制任务「等待 rec_XX 超时」但截图是战斗画面 | 录制时手动等的战斗没录进步骤。解释器发现下一步找不到按钮时会先判断是否在战斗，是就等它打完再重试（两种 HUD 都认，上限 10 分钟，全程可停） |
| 按「停止」像没反应 | 现在会打日志、按钮变「■ 停止中…」，录制的 `sleep` 分段可打断。另外连续 3 个任务失败会自动停手，剩下的标「跳过」 |
| 页面白屏并提示「灾难性故障」 | 渲染进程崩了（Unity + 高码率流顶爆显存）。重新加载，并把 `fps_high` 降到 30、`quality` 降到 50 |
| 任务卡在某个界面 | 多半是返回键。给那个页面录个 `btn_back`（见第五节） |
| 4K 屏字太小 | 已跟随系统缩放，不强制 1x。仍是 1x 就检查是否有旧进程在跑（`run.py` 只应有一个） |
| 看不到控制面板 | 默认就是独立面板；若把 `ui.overlay` 改成了 `true`，浮层被页面换 DOM 清掉时程序会在 30 秒后兜底开独立窗口 |
| 版本号点开说「仓库暂不可访问」 | 仓库私有或改名了，检查被跳过。等它重新 public 就会通；fork 的话把 `update.repo` 改成你的地址 |
| 更新后没重启起来 / 想退回旧版 | 先看 `logs/update.log`；旧版完整备份在 `update/backup/`，把里面的 `KyHelper_Web.exe` 和 `_internal/` 挪回上一级即可 |

运行日志和失败现场在 `logs/`（失败截图在 `logs/fails/`），出问题先看这两处。

---

## 十、风险

自动化违反绝大多数游戏用户协议。保留随机延迟、别 24 小时挂着、
别用于账号交易。

---

## 十一、支持作者

如果这个项目对你有帮助，欢迎请作者喝杯奶茶 🧋

<table align="center">
  <tr>
    <td align="center" width="300">
      <img src="docs/donate/wechat.png" width="210" alt="微信赞赏码"><br>
      <b>💚 微信赞赏</b>
    </td>
    <td align="center" width="300">
      <img src="docs/donate/alipay.png" width="210" alt="支付宝赞赏码"><br>
      <b>💙 支付宝赞赏</b>
    </td>
  </tr>
</table>

<p align="center">感谢你的支持 ✨</p>

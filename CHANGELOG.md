# 调试历史 / CHANGELOG

本文件记录每一次逻辑或功能的微调:**改了什么、为什么改、如何验证**。
最新的在最上面。运行时的详细日志见 `logs/app.log`(本文件是人类可读的变更史)。

---

## 2026-07-23

### 输出文件夹按中文标题命名(方便查找)

- **改了什么**:`long_video` 新增 `_chinese_title()`——生成前用 DeepSeek 给剧本起一个 6~12 字中文标题,经 `_slugify` 拼进输出文件夹名,如 `20260725_中国僵尸起源传说_long/`;任意语种剧本(中/英/日/韩)标题都统一中文。DeepSeek 失败或空标题时回退纯时间戳命名,不影响生成。
- **实现细节**:`_slugify` 本就保留 CJK,可直接用于中文文件名;json 模式偶发前置空白截断,max_tokens 提到 512 并要求紧凑输出、标题截断到 20 字。
- **为什么**:6 个视频全叫 `时间戳_long`,在 outputs 里无法辨认。
- **如何验证**:6 份 inputs 稿件分别得到「凡人成神之中国记忆 / 古人择墓地风水 / 中国僵尸起源传说 / …」等文件夹名。

### 新增日语 / 韩语两个语种

- **改了什么**:
  - 前端语言下拉新增「日本語 / 한국어」;后端 `main` 校验放开到 `zh/en/ja/ko`。
  - `long_video`:新增 `LANG_VOICES` 表(ja→`ja-JP-KeitaNeural`/say `Kyoko`;ko→`ko-KR-InJoonNeural`/say `Yuna`),`_resolve_video_cfg` 改表驱动;非中文语种(en/ja/ko)统一走通用系统提示(不再强制中式设定,由剧本推断),图像 prompt 仍为英文。
  - `video_builder` 字幕两处适配:① `_wrap_cjk` 按语言分流——中/日(全角无空格)逐字换行、韩(全角有空格)按词全角换行、拉丁按词 2x 换行;② 字幕字体按语言选(`SUBTITLE_FONTS`:ja=Hiragino Sans GB 含假名、ko=Apple SD Gothic Neo 含谚文),避免豆腐块。
- **为什么**:拓展日韩市场;日语无空格、韩语全角、二者字体与中文不同,不处理会换行溢出或渲染成方块。
- **如何验证**:实测烧录日/韩字幕帧,汉字+假名、谚文均正常渲染、2 行居中描边(见 scratchpad frame_ja/ko.png);四语种 voice/font/换行解析全部正确。

### 字幕分屏滚动:每屏最多 2 行(符合欧美观看习惯)

- **改了什么**:`build_long_video` 之前把整句旁白当成一条字幕,长英文句会一次铺 5–6 行。新增 `_subtitle_cues()`:先按语言换行(中文逐字/英文按词),再每 `subtitle_max_lines`(默认 2)行打包成一"屏";每句时长按各屏字符数比例分摊,字幕随语音**分段滚动**。音画总时长不变,保持同步。新增 `video.subtitle_max_lines` 配置。
- **为什么**:截图反馈英文字幕一次显示太多行,不符合欧美 1–2 行滚动的习惯。
- **如何验证**:竖版(max_chars=11)下,示例长句"When someone dies…human world." 由 6 行一屏 → 拆成 **3 屏 × 2 行**顺序显示;中文短句仍单屏、长句自动分屏。

### 新增语速两档(快速 / 适中·长视频)

- **改了什么**:前端设置区新增「语速」下拉(`fast` 快速=默认最快 / `slow` 适中·长视频);`GenerateRequest` 加 `speed` 字段并校验;`long_video.SPEED_RATES` 映射 `fast=+15%` / `slow=-5%`,在 `_resolve_video_cfg` 里覆盖 config 的 rate(中英文都生效,英文音色不受影响);CLI 增加第 5 个参数 `speed`。
- **为什么**:用户需要更慢的语速做 YouTube 长视频叙事,同时保留现在的快速档。
- **如何验证**:`_resolve_video_cfg` 对 fast/slow × zh/en 分别解析出 rate=+15%/-5%,英文档音色仍为 Christopher。
- **可调**:`long_video.SPEED_RATES` 里改两档具体数值即可。

### 停顿仍偏长:trim 升级为"全段静音压缩"(含句内停顿)+ 阈值 -35dB

- **改了什么**:`_trim_silence` 从"只切首尾"(start_periods=1 + areverse)升级为**单次 silenceremove 压缩全段所有静音**(`start_periods=1` + `stop_periods=-1`,`stop_duration=0.08`),把首尾**和句内逗号/句号处**的停顿都收紧到 `gap_seconds` 秒;阈值由 -45dB 提到 **-35dB**(抓住 TTS 句尾低音量拖尾)。新增 `trim_threshold_db` 配置;裁剪失败日志级别 debug→warning(便于发现"没生效")。
- **为什么**:上一版只切首尾,句尾 TTS 自带的低音量拖尾(在 -45dB 之下)没被切掉;且 `gap_seconds` 0.12→0.03 的差异仅 ~0.18s/句,用户"感觉没啥变化"。
- **如何验证**:实测用户风格短句裁剪:3.86→2.93 / 2.64→1.92 / 2.06→1.34 / 5.18→4.20s(每句砍 0.7–1.0s);`synth_narration` 走 config 默认后 5.18→4.32s。
- **注意**:必须**重启 uvicorn**(或确认 --reload 已重载 `video_builder.py`)——若 trim 未真正跑,改任何 `gap_seconds` 都不会有变化。

### 分镜数下拉改 1–25 全可选 + 配音语速 +15%

- **改了什么**:前端「分镜图数量」下拉由 5/10/15/20/25/30/40 改为 **1–25 逐个可选**(默认仍 25);`config.yaml` 的 `video.tts.rate` 由 `+0%` 提到 **`+15%`**(中英文均生效,英文路径经 setdefault 继承该值)。
- **为什么**:用户需要更细的分镜数控制(调试时用小数量)且反馈语速偏慢。
- **如何验证**:前端下拉可选 1–25;重启 uvicorn 后新生成的旁白语速加快。

### 解决"配音 AI 味太重":edge-tts 重试化 + 英文默认换低沉叙事声 + 422 可读化

- **改了什么**:
  - **根因**:`say failed: Voice 'Alex'` 的真正问题不是音色名,而是 edge-tts(微软神经语音,首选)一次性网络抖动就被 `except` 捕获、整段降级到机械的 macOS `say`。诊断确认 `.venv` 里 edge-tts 7.2.8 正常、能合成中英文 mp3,失败是临时性的。
  - `video_builder._edge_tts` 增加**重试**(默认 4 次,1.5s 起指数退避)+ **空文件校验**(edge 偶发"成功"却写 0 字节也算失败重试);`synth_narration` 仅在重试全部失败后才降级,并支持 `tts.allow_say_fallback=false` 强制"宁可失败也不用机械声"。
  - `long_video.EN_VOICE_DEFAULT`:`GuyNeural` → **`en-US-ChristopherNeural`**(低沉沉稳,叙事/恐怖旁白感更强);离线兜底 `EN_SAY_VOICE` 由不存在的 `Alex` 改为 `Samantha`。
  - 前端 422 校验错误可读化(拼 `loc + msg`,不再 `[object Object]`)。
- **为什么**:用户反馈成片配音机械感重——因为实际用的是 `say` 兜底而非神经语音;重试让临时失败不再毁掉整段音质。
- **如何验证**:`.venv` 内实测 edge-tts 合成 Guy/Andrew/Brian/Christopher 四款英文声均成功(~20KB mp3);语法/导入通过。完整成片需重启 uvicorn 后实跑。

### 精简为"稿件驱动"单一模式 + 画幅切换 + 英文剧本支持 + 前端界面重整

- **改了什么**:
  - **移除模式**:去掉"一句话生成故事"(frames/video/prompt 三种模式)与"人物一致 IPAdapter"开关,只保留"粘贴故事稿/播音稿 → 分镜图 + 配音字幕成片"。`main.py` 精简为单一入口(仅调用 `run_long_video`);移除 references / character_consistency / render_images / mode 参数。
  - **移除参考图**:前端删除拖拽上传参考图区,后端不再解码 references(去掉 base64/uploads 处理);IPAdapter 相关模块仍保留,但走不到。
  - **画幅切换**:请求增加 `orientation`(landscape=1920×1080 / portrait=1080×1920 Shorts);`long_video._resolve_video_cfg` 依据画幅覆盖 `out_width/out_height` 与 SDXL 出图尺寸(1344×768 / 768×1344);前端图片网格 aspect-ratio 与视频容器随画幅切换。
  - **英文剧本**:请求增加 `language`(zh/en);`long_video.py` 拆出英文版 STYLE_SYSTEM/PROMPT_SYSTEM(不再强制"Chinese/East Asian"),TTS 音色切换到 `en-US-GuyNeural`(离线兜底 `Alex`);`video_builder._wrap_cjk` 检测非 CJK 时按单词换行(而非逐字),字幕英文长句正常换行。
  - **前端界面重整**:配置改为四格网格(语言/画幅/风格/分镜数),布局更整齐;去掉多个 checkbox 开关;示例主题词 chips 移除(输入区改为一个大 textarea);美术设定折叠面板样式统一。
- **为什么**:用户不需要 AI 自编故事,只用已有稿子;参考图功能实际不稳定(整图特征注入难以精确);需要覆盖 YouTube 长视频与 Shorts 两种投放尺寸;英文稿件之前会被强制翻中文/配中文旁白。
- **如何验证**:`python3 -c "ast.parse(...)"` 三个后端文件语法通过;前端 index.html 已在预览面板可打开;完整生成需 GPU 环境实跑(未跑)。

## 2026-07-22

### 图片调试模式 + 中文约束翻译 + 参考图 IPAdapter + 拖拽上传 + 可选风格档
- **图片调试模式**(`debug_images.py`,mode=prompt):只按正向描述出图,跳过故事/分镜;N 张变体,存 prompt.txt;网页「🧪 图片调试」入口。
- **中文约束翻译**:`to_english_fragment()` 把含中文的正/负向约束自动翻成英文关键词(含则调 DeepSeek,纯英文/空直接返回),让中文负向也真正生效。
- **参考图 IPAdapter**:图片调试模式支持上传多张参考图,`comfy_client._build_ref_workflow` 动态生成 IPAdapter 链(每图一节点+权重);描述里"图N=人物/环境"解析为权重;下载通用 ip-adapter-plus_sdxl 模型。
- **拖拽上传**:参考图区支持拖拽/点击,缩略图预览带 图1/图2 编号与删除。
- **可选风格档**(`STYLE_PROFILES`):horror(默认,故事画风+恐怖尾巴)/ realistic(纪实摄影,通用题材不跑偏)/ none(纯描述);网页「风格」下拉,贯穿所有模式。
- 默认分镜图数量由 10 改为 1。
- **为什么**:纯文字对具体 IP(九叔/僵尸)无能为力需喂基准图;恐怖风格尾巴会把通用题材(80年代农民)带偏,需要可切换的写实档。
- **已知局限**:IPAdapter 是整图特征注入,一次难以"双人精确同框进新场景"(需区域遮罩);内联"图N=角色"解析无分隔符时会误伤正文(待改成缩略图下拉)。

## 2026-07-21

### 新增画面约束 / 美术设定(约束年代、服饰、环境、道具)
- **改了什么**:新增全局"美术设定"(正向+负向),两层入口(config.yaml `art_direction` 默认 + 网页可折叠输入框覆盖),双管注入——① `art_note()` 把正向约束喂进 DeepSeek 规划(story/scene/prompt/短片/长片);② 兜底拼进最终 SDXL 正向 prompt,负向拼进 negative。覆盖全部模式;API/各 run 函数加 `art_positive/art_negative` 参数。
- **为什么**:生成图常出现年代/服饰/环境不符(如古装、现代穿帮),需要用户可控的世界设定约束。
- **如何验证**:dry-run 确认 art_note 指令块生成、最终正向=场景+约束+画风、负向=基础+负向约束;网页「美术设定」区可展开填写。
- **已知点**:负向仅拼给 SDXL(英文有效,中文无效);正向中文可经 DeepSeek 生效但 SDXL 兜底层不识中文。

### 视频优化:16:9 画幅、大幅压缩、字幕换行、加快语速
- **改了什么**(video_builder + config.video):
  - 输出改 **1920×1080 (16:9)**,视频模式图片按 **1344×768** 生成(不再 1:1);video_builder 全面改用 out_width/out_height。
  - 压缩:crf 20→**28**、fps 30→**24**、胶片颗粒 7→**3**;10 分钟视频从 ~2.1GB 降到 ~55MB(码率 28Mbps→0.77Mbps)。
  - 字幕:长片从 SRT 改 **ASS 并声明 PlayResX/Y=视频分辨率**(修复 libass 把字号按 288 参考系放大 3.7 倍的问题),`_wrap_cjk` 按标点自动换行,长句不再超出画面。
  - 语速:edge-tts rate `-8%`→**`+0%`**(更快,~10 分钟)。
- **为什么**:用户反馈文件过大、画幅应为 YouTube 16:9、字幕超框、语速偏慢。
- **如何验证**:复用现有资产快速合成 —— 输出 1920×1080、32.6s=2.9MB(折合 10 分钟~55MB)、抽帧确认长字幕换行且完整在框内。

### 新增长片模式:朗读稿驱动的 ~10 分钟叙事视频(25 张分镜)
- **改了什么**:
  - 新增 `backend/long_video.py`:输入完整朗读稿 → 逐句 edge-tts 配音(时长驱动)→ 按累计时长切成 N 组(边界法,正好 N 组)→ DeepSeek 生成风格圣经+角色描述+每组一条英文 prompt → ComfyUI 出 N 张图 → 合成。
  - `video_builder.build_long_video`:每张图时长=其旁白组时长,**硬切拼接保证音画同步**,逐句 SRT 字幕经 libass 烧录(已验证中文渲染正常)。
  - 网页新增「📖 长片模式」(输入框粘朗读稿,分镜数即关键图数);分镜数下拉加 15/20/25;后端 `mode=long`,图数上限放宽到 40。
- **为什么**:此前只能让 AI 编短故事;用户需要用自己的长播音稿产出 10 分钟成片。
- **如何验证**:干跑分组——206 行/2160 字 → 正好 25 组,每组 ~19-27s,预估总时长 9.6 分钟;libass 中文字幕烧录抽帧正常。**尚未跑完整生成**(25 张 GPU 图约 46 分钟)。

### 配音升级为 edge-tts 神经语音(替代机械的 macOS say)
- **改了什么**:`video_builder.synth_narration` 默认改用 edge-tts(微软神经语音,免费无需 key),默认音色 `zh-CN-YunjianNeural`(小说/旁白男声)、语速 -8% 更阴森;macOS `say` 降级为离线兜底。config 的 `video.tts` 段可配 engine/voice/rate/pitch。
- **为什么**:婷婷是老式拼接语音,AI 机械感重;神经语音自然度显著提升。
- **如何验证**:单测 edge-tts 生成 mp3 正常;用 3 张现成图重建带配音视频(12.2s,视频+aac 音轨、音画同步无误)。联网失败会自动回退 say。

### 图生视频 V3-lite:Ken Burns 动效 + 讲述者配音 + 字幕
- **改了什么**:
  - 新增 `backend/video_builder.py`:程序化动效(Ken Burns 推拉/平移,5 种预设)+ 交叉淡入 + 恐怖氛围叠加(暗角/颗粒),用 pip 自带的静态 ffmpeg(不依赖 brew);`build_video()` 出无声动效视频。
  - Phase 2:`synth_narration()`(macOS `say -v Tingting` 中文配音)、`_render_subtitle_png()`(Pillow + STHeiti 渲染中文字幕,白字黑边)、`build_narrated_video()` 按场景把配音/字幕/画面同步(每句旁白时长决定该镜头时长)。
  - 新增 `backend/short_video.py`:theme → DeepSeek 极短故事(N 镜头,每镜头 prompt + ≤18 字旁白)→ ComfyUI 出图 → 带配音字幕的成片;config 增加 `video` 段。
- **为什么**:走程序化动效而非扩散图生视频,M1/M3 无 GPU 压力、可反复调;先做"极短故事配极短视频"验证全链路。
- **如何验证**:① 用 3 张现成图单测 Ken Burns + 配音 + 字幕,产出 7.7s 视频(含 aac 音轨,字幕正确渲染);② 跑《走夜路见红影》3 镜头完整成片。

### 分镜图数量控制 + IPAdapter 人物一致(V2)
- **改了什么**:
  - 前端增加「分镜图数量」下拉(1–10)与「人物一致 (IPAdapter)」开关,并显示角色参考图;网格改为按实际帧数渲染。
  - API/pipeline 增加 `num_frames`、`character_consistency` 参数;新增 `_generate_character_refs()` 先为每个角色生成参考肖像,再按场景中出现的角色用 IPAdapter 条件化每一帧。
  - comfy_client 增加 `upload_image()` 与 `reference_image` 支持,新增 IPAdapter workflow `sdxl_ipadapter.json`;config 增加 `nodes_ipadapter`、`ipadapter_weight`。
  - ComfyUI 安装 `ComfyUI_IPAdapter_plus` 自定义节点 + 下载 CLIP-ViT-H(2.4G)与 ip-adapter-plus-face_sdxl(808M)。
- **为什么**:① 全 10 张渲染太慢,调试时需要先出 2–3 张快速验证;② 实现 V2 路线的人物一致(此前每帧人脸都不同)。
- **如何验证**:界面选「分镜数=2 + 人物一致」跑《雨夜轮回》→ 生成 2 张角色参考图 + 2 帧,均以司机(王强)为参考;对比参考肖像与 frame_01,人脸/发型/胡须/衣着一致(468.5s,无报错)。

### 建立统一调试日志系统
- **改了什么**:新增 `backend/logging_config.py`(控制台 + 滚动文件 `logs/app.log`);在 deepseek_client / comfy_client / pipeline / main 各层埋点;`config.yaml` 增加 `logging` 段;`logs/` 加入 `.gitignore`。
- **为什么**:此前全靠 `print()`,无法留痕、无法按级别过滤、排查性能问题(如单帧耗时)困难。
- **如何验证**:`LOG_LEVEL=DEBUG` 跑纯文本,控制台与 `logs/app.log` 均输出结构化日志;确认记录到 DeepSeek 耗时/token、ComfyUI 每帧 prompt/seed、单帧渲染耗时(frame_01=112.8s)。

### 网页界面增加「停止」按钮(支持渲染中打断)
- **改了什么**:comfy_client 增加 `interrupt()` 与渲染中 `should_cancel` 检测;pipeline `run()` 增加 `should_cancel` 钩子(阶段间 + 每帧前检查);main.py 增加 `POST /api/jobs/{id}/cancel` 与每 job 的 `threading.Event`;前端增加停止按钮与 `cancelled` 状态处理。
- **为什么**:图片渲染耗时长,用户需要中途停止;此前只能手动去 ComfyUI 打断,导致缺帧且状态不同步。
- **如何验证**:浏览器实跑渲染,渲染到第 4/30 步时点停止 → ComfyUI 日志 `Global interrupt / Processing interrupted`、队列清空、页面切到「已停止」并保留已完成帧。

### 网页界面展示完整剧情 + 分镜脚本
- **改了什么**:前端故事面板增加「剧情」章节列表与「分镜·镜头脚本」面板;main.py 的 job 状态增加 `prompts` 字段。
- **为什么**:原界面只显示标题/logline/角色,看不到完整故事线和每个镜头的最终 prompt。
- **如何验证**:文本模式生成《山村怨灵》,页面完整渲染 8 章剧情与 10 个分镜(含镜头标签与英文 prompt)。

### 新增网页界面(FastAPI + 前端)
- **改了什么**:重写 `backend/main.py`(异步任务 + 轮询,静态托管图片);新增 `frontend/index.html`(输入框、四阶段实时进度、故事面板、图片网格);pipeline `run()` 增加 `on_event` 进度回调。
- **为什么**:此前只能命令行传主题,需要可视化的输入与进度展示。
- **如何验证**:`uvicorn` 起服务(端口 8000 被占,改 8010),浏览器输入主题生成,进度条与产物正常。

### 故事默认中文背景
- **改了什么**:story_generator / scene_planner / prompt_generator 三个系统提示加入「中文设定」约束——人物中文名 + 英文描述且以 `a Chinese woman/man` 开头,场景中式。
- **为什么**:SDXL 默认画西方脸/西式场景;首版《午夜医院》角色是西方脸(Nurse Sarah)。
- **如何验证**:重跑《午夜护士》,story.json 角色为「林薇 / a Chinese woman...」,渲染出的护士为东亚长相。

### 初始化项目脚手架
- **改了什么**:建立 `ai-horror-generator` 项目:DeepSeek 客户端、故事/分镜/prompt 三段 agent、ComfyUI 客户端、SDXL workflow、pipeline 编排、config.yaml、README;`.env` 管理 DeepSeek key。
- **为什么**:实现「一句主题 → 结构化故事 + 分镜 + 10 张风格统一关键帧」的 V1 目标。
- **如何验证**:全链路实跑《午夜医院》,产出 story/scenes/prompts.json + 10 张关键帧(9/10 成功,1 帧被手动中断)。

### 环境搭建
- ComfyUI 安装于 `~/learnArea/ComfyUI`(用 uv 装独立 Python 3.11,绕过 brew/Xcode CLT),PyTorch 2.13 + MPS;下载 SDXL base 模型(6.94GB)。

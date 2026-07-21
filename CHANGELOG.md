# 调试历史 / CHANGELOG

本文件记录每一次逻辑或功能的微调:**改了什么、为什么改、如何验证**。
最新的在最上面。运行时的详细日志见 `logs/app.log`(本文件是人类可读的变更史)。

---

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

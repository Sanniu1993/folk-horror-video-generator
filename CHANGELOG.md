# 调试历史 / CHANGELOG

本文件记录每一次逻辑或功能的微调:**改了什么、为什么改、如何验证**。
最新的在最上面。运行时的详细日志见 `logs/app.log`(本文件是人类可读的变更史)。

---

## 2026-08-05

### 开源发布准备

- **改了什么**:重写 README(定位、现状横幅、功能、快速开始、已知局限、路线图)、新增 MIT LICENSE、
  docs/demo_frames.jpg 演示截帧;与姊妹仓库互相链接;.gitignore 排除 .idea / BGM 音乐文件。
- **为什么**:项目被社区推荐,需要可公开的仓库地址。
- **如何验证**:全工作区与 git 历史扫描无密钥(仅 .env.example 占位符);.env / 数据库 / 上传图未入库。

### 服饰纠偏补帽子词:压住了衣服,没压住韩式笠帽

- **怎么发现的**:用户看封面画质实测图时指出"服饰错误,搞成韩国服饰了"。属实。
- **两件事要分开**:(1) 那次实测的负向是我从 `config["negative_prompt"]` 直接取的,
  **漏了管线真正会加的 `CJK_COSTUME_GUARD`**([long_video.py:413](backend/long_video.py) 对
  `language == "zh"` 无条件追加),所以那张图不代表管线水平 —— 测试脚本的锅;
  (2) 但补上 guard 重跑之后暴露了一个**真 bug**:同一条 prompt,袍子从交领改对成了圆领
  中式蓝袍,**帽子仍然是韩式笠帽**(宽平檐 + 圆顶 + 红缨帽带)。原因是 guard 里只有
  `hanbok, Korean clothing` 这类"衣服"词,**一个帽子名都没有**。
- **改法**:`CJK_COSTUME_GUARD` 补 `gat, jeonmo, Korean hat`(韩式笠帽)和 `jingasa`
  (日式阵笠)。补完出的是清代瓜皮帽 + 红顶缨,对了。
- **守住原有的取向**:仍然只列日/韩**特有**的词。特别是 **不能** negate
  `conical hat / straw hat` —— 斗笠是中国农民、挑夫、赶尸匠的正当装束,压掉就画不出来了。
  (实测那张 D 的负向里我一次加了好几个帽子词,所以无法断定是哪个词起的作用;
  代码里按上面这条原则只收日韩专有名。)
- **影响面**:不止封面。分镜里只要出现"道士 / 端公 / 老人"这类词就可能戴上这顶帽子,
  而 94% 的镜头都有人 —— 这个漏洞一直在生效,只是没人盯帽子。

### 空镜配额:94% 的镜头里都有人,纯环境镜只剩 6%

- **怎么发现的**:用户反馈"生成的图 80% 都是人物图,纯场景图很少"。统计最近 8 轮
  共 125 个分镜的 `long_plan.json`:

  | | 镜数 | 占比 |
  |---|---|---|
  | 画面里有人 | 117 | **94%** |
  | 纯环境空镜 | 8 | 6% |
  | 景别是 wide / establishing | 7 | 6% |

  景别分布同样偏:`medium close-up` 30%、`medium shot` 17%、`wide shot` 只有 5%。
- **为什么**:不是模型跑偏,是提示词只说了一半。`PROMPT_SYSTEM_ZH/EN` 里有 **4 条**
  把镜头往人身上推的规则(「有人物的镜头一律用 close-up 或 medium close-up」、
  「别老是全身远景」、英文版同义两遍),却**没有任何一条**规定"多少镜可以没人";
  而每镜又和一段讲人的旁白一一对应,LLM 收到的净信号就是「人物 + 近景」。
  `front_load_shot_size` 把景别拎到句首并加权 1.25,加权的多数是 `medium close-up`,
  把这个偏差又放大一档。讽刺的是 `mem_env`(纯"地点+建筑+时段")一直在产出,
  却从没变成过一张属于自己的图。
- **① 规划阶段下硬配额**:新增 `ENV_QUOTA_ZH/EN` 配额块,按 (n, n_env) 渲染后塞进
  `PROMPT_SYSTEM_*` 的 `{env_block}`。写明"本批 {n} 镜必须有 {n_env} 镜是纯环境空镜"、
  该挑哪些段落(开场/地点时间跳跃/高潮前一拍/结尾/只交代背景的段落)、绝不要挑哪些
  (关键动作、惊吓、对话)、画什么(建筑陈设天气光线 + 人留下的痕迹),以及空镜的
  `mem_query` 只写环境词。**下数量指标而不是继续劝**:现有那 4 条劝导式规则就是反证。
- **配额按全片算再分摊到各批**(`prompt_generator.env_quotas`):提示词是分批要的
  (`PROMPT_BATCH=6`),但不能让每批各自 `round(自己的镜数 × 0.25)` —— 末批常只剩 1-2 镜,
  那会要求它 50% 是空镜。改为全片算总数后轮转分摊,单批上限为批内镜数的一半。
  20 镜 -> `[2,1,1,1]`,15 镜 -> `[2,1,1]`。
- **② 空镜 prompt 复用 `mem_env`**:它本就是"地点+建筑+时段",翻成英文加 `wide shot`
  + 该镜影调就是一条现成的空镜 prompt,零额外产出成本。
- **③ 出图前的确定性兜底**(`prompt_generator.ensure_env_shots`):像
  `front_load_shot_size` 一样不依赖模型自觉 —— 空镜数不够配额,就按 `mem_env` 把最合适
  的几镜确定性改写成纯环境镜。规则:不动 `mood=shock`(那是全片最重要的一击);
  越静的 mood 越优先(calm > reveal > sad > eerie > tension);同 mood 时 `mem_env` 词多的
  优先;不让两个空镜相邻;`mem_env` 词集重合过半的候选跳过(实测"农村 土房 室内 白天"
  和"农村 土坯房 堂屋 白天"会同时被选中,出两张雷同空镜);中文 `mem_env` 用
  `to_english_fragment` 翻译(通常 0~2 次调用),翻不动就跳过这镜,不硬塞中文进 SDXL。
  改写时**同步把 `mem_query` 换成 `mem_env`** —— 否则会检索回人物素材的标签
  (长发白裙…)注进一间空房间。
- **④ 空镜靠负向压人,不靠正向说"没有人"**:扩散模型不理解正向里的否定,写 "no people"
  反而会把人召唤出来。新增 `NO_PEOPLE_NEGATIVE`,出图时按镜头本体判断,是空镜就追加到
  该镜的负向(`comfy.generate` 本来就是每镜一个 negative)。重出(评审闸口)也沿用,
  否则重出一次人就回来了;用户手改了 prompt 时按新 prompt 判断(他可能就是要把人加回来)。
- **`has_people` 的取向:宁可多判不可漏判**。多判(没人的镜被当成有人)只是少加一条
  负向,无害;漏判会给真要画人的镜头加上 no-person 负向,直接和正向打架。
  两个必须注意的坑:(1) 先摘景别再判 —— `head and shoulders`、`full body shot`、
  `medium shot` 这些景别自带 head/body,不摘的话每条 prompt 都会被判成有人;
  (2) 词表第一版漏了 `youths` / `residents`,把"1960s Chinese educated youths lowering a
  coffin"和"Residents carrying cardboard boxes"判成了空镜 —— 已补齐职业/亲属/年龄段词。
- **配置**:`config.yaml` 新增 `env_shot_ratio: 0.25`,设 0 则整段配额块不发给模型
  (避免"必须有 0 镜是空镜"这种自相矛盾的提示)且兜底不生效,完全恢复旧行为。
- **怎么验的**:拿 8 轮历史 `long_plan.json` 当回放数据跑兜底(见
  `scratchpad/test_env_shots.py`):**空镜 8/125 (6%) -> 31/125 (25%)**,配额全部达成;
  不变式全绿(不动 shock 镜 / `mem_query` 同步换成环境词 / 新空镜不相邻)。
  最新一轮用真 DeepSeek 翻译验产出质量,影调尾巴正确沿用:
  `wide shot, rural, rammed earth house, wooden door, doorway, night, cold blue shadows`。
  检测器另在 7 个人工样例上校验(含加权景别 `(medium close-up:1.25)` 的形式)。
  **注意**:第一版假翻译回带中文被 `_has_cjk` 判成翻译失败,导致误报"0 改写" ——
  测试桩必须返回纯 ASCII。
- **顺带修掉一个被空镜配额放大的旧 bug**:`comfy_client` 里本来还有一份独立的
  `has_person` 词表(门控 hires 和 FaceDetailer 的 `only_with_people`),两个方向都错:
  它把 `medium` 和 `body` 收进词表却**不摘景别**,于是
  `medium shot, inside a torn greenhouse, dead chickens`、
  `medium close-up, rusted iron nails in a tree trunk` 这类纯环境镜全被判成有人,
  白跑一次 FaceDetailer(每张约 30s);反过来又漏了 youths / residents / hands。
  以前 94% 的镜头本来就有人,这个 bug 一直没显形 —— 空镜配额上线后每轮都会白烧几张的
  时间。现在 `comfy_client.has_person` 直接改成 `prompt_generator.has_people` 的别名,
  只留一份实现。("宁可多判不可漏判"这个取向对修脸门控同样成立:多判只是多花一次修脸,
  漏判是脸糊。)顺带删掉 `comfy_client` 里已无人使用的 `import re`。
- **配额块本身也做了 A/B**(同一剧本同一批 6 段,只换 system,见
  `scratchpad/test_quota_llm.py`):**旧提示词 0/6 空镜,新提示词 2/6,正好等于配额**。
  产出的空镜是「人留下的痕迹」那条指令在起作用,而且自带剧情信息:
  `wide shot, empty rural house interior, thick hemp rope hanging from a main roof beam,
  overturned wooden stool...`(上吊现场的空镜),`mem_query` 也确实只有环境词。
  LLM 自己把两个空镜放在第 1 和第 4 镜,没有相邻。
  说明这条路是**规划阶段的配额为主、确定性兜底为辅**,兜底大概率很少触发。

---

## 2026-08-01

### 提速:70 分钟 -> 约 26 分钟(先量再改,四处各治一段)

- **为什么**:用户反馈 5K 字的稿子出一个 14 分钟视频要 70 分钟。先从日志量出真实分布
  (`20260804` 那次 20 张图、416 句、总 4246s):
  | 阶段 | 耗时 | 占比 |
  |---|---|---|
  | TTS 配音 | 18.1 min | 26% |
  | 分镜规划(DeepSeek) | 6.0 min | 8% |
  | 出图 20 张 | 41.8 min | 59% |
  | 合成视频 | 4.8 min | 7% |
- **① TTS 串行 -> 并发(18.1 -> ~3.3 min)**:单句 = 一次网络往返 + 两次 ffmpeg 子进程,
  本地几乎不吃 CPU,416 句串行纯属干等。新增 `video_builder.synth_narration_batch()`
  (线程池,按下标回填保序;取消时未完成的返回 None 交调用方 `check_cancel` 收尾;
  任一句真失败仍照旧抛出)。`video.tts.workers` 默认 8。
  **实测 40 句:104.4s -> 18.9s(5.5 倍)**。
- **② 分镜规划 4 批串行 -> 并行(6.0 -> ~1.5 min)**:各批本来就互相独立,推理模型单批
  约 90s,并行后约等于最慢那批。
- **③ 出图单帧 138.9s -> 56.4s(hires 默认关)**:装了 FaceDetailer 之后"脸糊"已由它
  单独解决,hires 剩下的作用只是整图纹理。同镜头实测:
  hires+FD `138.9s`/1552x864、**只 FD `56.4s`/1152x640(脸依旧锐利)**、
  只 hires `62.5s`、都不开 `24.3s`。故 `generation.hires.enabled` 默认改 **false**。
  代价:背景/墙面纹理略软(成片上采到 1080p 从 1.24 倍变成 1.67 倍);要画质改回 true。
- **④ x264 preset medium -> faster(4.8 -> ~2.5 min)**:成片要经「每镜头片段 -> 转场链
  -> 烧字幕」三轮编码,单轮快约 1.5-2 倍,同 crf 下文件略大、肉眼无差。
  新增 `video.x264_preset`,想要小文件改回 medium。
- **合计预估**:70.8 min -> **约 26 min**(TTS 3.3 + 规划 1.5 + 出图 18.8 + 合成 2.5)。
  出图仍是大头且**无法并行**(单 GPU 串行),要再快只能减张数或降分辨率。
- **顺带修**:评审闸口的测试打桩点失效 —— `synth_narration_batch` 内部调用的是
  `video_builder.synth_narration`,不再是 `long_video` 命名空间里的那个。测试已改打桩点,
  重跑全绿(生产代码无此问题,仅影响 monkeypatch 式测试)。

### 新增:FaceDetailer 人脸精修(治多人镜头里配角的脸糊)

- **为什么**:景别修好之后主体的脸清楚了,但**多人镜头里非主体的那张脸仍然发软**。
  hires 是整图放大重采样,只能把已经画出来的脸画实,算力仍然集中在主体上,配角救不回来。
  FaceDetailer 用 YOLO 检出画面里**每一张**脸,逐张放大到 guide_size 单独重绘再无缝贴回,
  配角能拿到和主角一样的像素预算。
- **装了什么**(用户批准):
  - `custom_nodes/ComfyUI-Impact-Pack`(V8.28.3)+ `ComfyUI-Impact-Subpack`(V1.3.5)
  - 依赖经 `uv pip` 装进 ComfyUI 的 venv,**用 constraints 锁死 torch/torchvision/numpy/
    transformers/pillow**;dry-run 确认只新增 22 个包、不动任何既有包,装完复核
    torch 2.13.0 / torchvision 0.28.0 / numpy 2.4.6 / MPS 可用 均未变。
  - **跳过了 `git+facebookresearch/sam2`**(Impact Pack requirements 里的重依赖):
    代码里用 `find_spec` 保护、只有 SAM2 专属节点才需要,FaceDetailer 走 bbox 检测器即可。
  - 人脸检测模型 `models/ultralytics/bbox/face_yolov8m.pt`(50MB,Bingsu/adetailer)。
  - 重启了 ComfyUI(PID 34814,已跑 12 天;重启前确认队列为空)。节点注册成功,
    检测器下拉里能看到该模型。
- **改了什么**(代码):
  - `comfy_client` 新增 `_face_cfg()` / `_add_face_detailer()`:在解码之后、SaveImage 之前
    插入 `UltralyticsDetectorProvider` + `FaceDetailer`,改接 SaveImage 的输入。
    clip/vae 来源从工作流里现有节点上照抄(不写死,免得漏掉 LoRA 之类的链路)。
    基础链路与 IPAdapter 链路都支持,默认只对有人物的镜头生效。
  - `config.yaml` 新增 `generation.face_detailer` 段,注释写明各参数含义与红线。
- **如何验证**:双人夜景同 seed 对比 ——
  - 确认真的执行了:两图差异 bbox = (461,139)-(917,354),**正好是两张脸的位置**;
    ComfyUI 日志出现两次 `[Impact Pack] vae encoded/decoded`(对应两个人脸裁切)。
  - 放大次要人脸:改前皮肤蜡感、嘴里是一团黑;改后皮肤纹理、睫毛、牙齿都出来了,
    且**人物身份没被改掉**(denoise 0.4 的作用)。
  - 接线回归:有人物 -> 插入 FaceDetailer 并改接 SaveImage;无人物 -> 工作流与改动前一致;
    IPAdapter 链路同样正确插入。
- **红线**:`denoise` 不要超过 0.5 —— 再高会把脸重绘成另一个人,跨镜头的人物一致性就没了。

### 变更:民俗视觉记忆库改为默认启用

- **为什么**:用户要求。此前三处都默认关(config / API / 前端勾选框),每次都要手动勾。
- **改了什么**:
  - `config.yaml` `visual_memory.enabled` false -> **true**;`main.py` 的
    `use_memory` 默认 -> **true**;前端勾选框默认勾上,说明文字同步。
  - `memory_client` 新增 `is_reachable()`(探 `GET /health`);`long_video` 在开跑前探一次:
    连不上就**本轮整体关闭**并给页面一条提示。默认开之后"服务没起"会变常见,不先探
    就会每个分镜白发两次检索请求、各刷一条 warning。
- **如何验证**:三处默认值均为 true;把 base_url 指到死端口跑一轮 —— 出图照常完成、
  日志只留一条明白话、页面提示"民俗视觉记忆库没连上…不影响这次出图",无 warning 刷屏。
- **注意**:对接契约里原本写着"默认关",这条已作废;但 **fail-open 仍是硬约束** ——
  默认开的前提就是服务缺席必须无痛降级。已同步更新记忆。

### 修复:脸还是糊 —— 景别词写在句尾等于没写

- **为什么**:用户反馈新生成的视频人脸仍然模糊。先排除了修脸没生效:该次 8 张图全是
  1552x864、日志逐张都有「hires 二次采样已启用」,说明二次采样正常。真正的原因是**景别**:
  分镜提示词末尾明明写着 `medium shot` / `close-up`,出图却全是**全身站姿**,脸只占画面
  高度约 1/10 —— 再怎么放大重采样也救不回几十像素的脸。
  根因是上一轮「影调跟随剧情」把景别和影调一起放到了 prompt 末尾,而这条 prompt 有约
  100 词,SDXL 的 CLIP 按 77 token 分块,句尾的构图词权重极低。**构图必须写在最前面。**
- **改了什么**:
  - `prompt_generator` 新增 `front_load_shot_size()`:把景别词从任意位置拎到句首并加权
    (`(medium shot:1.25), ...`,ComfyUI 的 CLIPTextEncode 支持该语法)。
  - `PROMPT_SYSTEM_ZH/EN` 改为要求**每条 prompt 以景别开头**,句尾只留时间/光源/色调;
    并明确"有人物的镜头一律用 close-up 或 medium close-up"。
  - `long_video` 在拼最终提示词与「评审重出」两处都过一遍 `front_load_shot_size()` 兜底,
    LLM 没照做也能纠正。
- **如何验证**:取上一次那张"写了 medium shot 却出全身"的镜头,同 seed 对比 ——
  改后人物从走廊尽头的全身像变成半身,脸的线性尺寸约为原来的 3 倍,五官/皱纹清晰可辨。
- **遗留**:多人镜头里**非主体的那张脸**仍然偏软(整图放大重采样对次要人脸帮助有限)。
  彻底解决需要 FaceDetailer(逐张人脸裁切重绘),依赖 ComfyUI-Impact-Pack + 人脸检测模型,
  本机尚未安装。

### 修复:页面上选的分镜数被后端悄悄改掉

- **为什么**:用户反馈"设置的分镜图数目没生效"。查日志确认——21:04 那次**选了 8 张,被自动
  提到了 17 张**。是前面「分镜粒度兜底」默认开启导致的:它按 `max_seconds_per_image`
  自动补足张数,虽然记了日志,但页面上没有任何提示,看起来就像设置失灵。
  这个默认值定错了:用户明确选的数字应该说了算,不能让出图数量和总耗时在他不知情的
  情况下翻两倍。
- **改了什么**:
  - `long_video`:默认改为**只提示、不修改**。超出建议粒度时发一条 `notice` 事件
    (含"旁白 X 秒 ÷ N 张 = 每张 Y 秒""建议调到 M 张"),分镜数保持用户所选。
  - `config.yaml` 新增 `video.auto_adjust_frames`(默认 **false**);设 true 才恢复自动补足。
    `max_seconds_per_image` 从"自动补足的阈值"降格为"给建议的阈值",设 0 则连提示也不给。
  - `main.py`:job 增加 `notice` 字段;`frontend`:新增黄色提示条(与红色错误区分),
    开新任务时清空;按钮下方的说明改为"选几张就出几张"。
- **如何验证**:6 句 x 40 秒 = 240 秒的稿子选 3 张 —— 实际就出 **3 张**(不再被改),
  页面提示"旁白共 240 秒 ÷ 3 张 = 每张约 80 秒…想更贴合剧情可把分镜数调到 10 张左右"。

### 变更:去掉全片统一的风格设定,改为**影调跟随剧情**(每镜自己判断)

- **为什么**:用户提出"风格要为故事服务,应该跟着情节判断"。原来 style_bible 是全片一套、
  拼到每一张图上,结果白天的日常段落也被套上恐怖片暗调(上一条里"小区门口围观人群"被
  画成废墟就是这么来的)。恐怖感应该来自内容,不是来自给整片盖一层滤镜。
- **改了什么**:
  - 分镜提示词(`PROMPT_SYSTEM_ZH/EN`)接管影调:每条 prompt 末尾自带该镜的
    【时间/光源 + 色调 + 景别】3-6 词,按该段情节与 mood 选(白天日常→自然光/纪实感,
    夜里不安→单一路灯/冷蓝阴影,惊吓→硬光手电/高反差,回忆→柔和暗光/暖调);
    明确要求"别把整片都写成阴森暗调,白天就是白天"。prompt 上限 35→45 词。
    同一地点的建筑/陈设仍由 world 保持一致,变的只是光线与情绪。
  - `STYLE_SYSTEM_ZH/EN` **删掉 style_bible 字段**(不再产出全片画风/色调),
    只留 world / anachronisms / characters;`long_plan.json` 同步不再记 style_bible。
  - `prompt_generator`:新增 `auto` 档并设为默认 —— 全局只拼与情绪无关的介质词
    (`config.base_style_tail`,默认 "photorealistic, film grain, highly detailed",
    留空则一个字都不加)。`horror` / `realistic` / `none` 保留为手动覆盖。
  - `main.py` 请求默认 style 改 `auto`;前端下拉新增「跟随剧情(默认)」,
    原来的恐怖档保留为「全片统一恐怖调」。
- **如何验证**(真实 DeepSeek 跑 17 镜规划):全局尾巴只剩 5 个词;每镜影调按 mood 分化 ——
  calm→`overcast daylight, natural colours`、eerie→`dim flickering light, cold blue shadows`、
  sad→`soft dim light, muted warm tones` / `cool grey twilight`、
  reveal(室内)→`warm indoor lamplight`;景别也在中景/近景间变化。

### 修复:分镜提示词被 max_tokens 截断(改为分批请求)

- **为什么**:上面的改动让每镜要输出 prompt(≤45词)+ mood + mem_query + mem_env 四个字段,
  一次性要 25-40 条必然超出输出上限 —— 实测**只有 5 条**就到 5.7k 字符,重试到 16384
  仍被截断。被截掉的镜头会退回"拿整段中文旁白当 SDXL 提示词",画面必然跑偏。
- **改了什么**:`long_video` 分镜提示词改为**每批 6 条**(`PROMPT_BATCH`)分多次请求,
  批内段落重新编号 1..m、合并时加回偏移量(让模型数小数字不容易错位),
  每批仍各自享有截断抢救。单批预算给到 12288。风格调用的预算也从 8192 提到 16384
  (max_tokens 是上限不是消耗,给高不额外花钱,但能省掉一次白跑的重试)。
- **如何验证**:17 镜真实跑通,分 3 批全部返回,全局序号 1..17 连续无错位、无缺条。

### 修复:该命中的素材没注入 + 正常场景被画成废墟(三个叠加原因)

- **为什么**:用户反馈某镜应该是小区高层住宅楼,记忆库里明明有对应素材(密集高层住宅群),
  RAG 面板却显示「命中 3 · 采用 0 无注入」,出图还成了废弃楼。查 `20260801_210420` 这次运行:
  1. **检索词把人和事件混了进去**。`mem_query="小区楼下 人群围观 居民楼 白天 2020年 事故"`,
     库里没有"围观事故"这类素材,这些词把向量整体拉偏:
     | query | 密集高层住宅群 |
     |---|---|
     | 线上实际(混人+事件) | **0.4633**(被 0.55 挡掉) |
     | 只留环境 | **0.5756**(能过) |
  2. **全局风格尾巴带着"场景状态词"**。style_bible 的
     `texture: concrete, rusted metal, damp grime, brick dust` +
     `lighting: dim stairwell shadows, black voids` 拼到**每一张**图上,镜头本体写的是
     "Summer afternoon, crowd gathered",模型只能往废墟解。
  3. **注入的标签本身自带破败**。修好检索后,环境路把「荒废筒子楼 0.5646」「斑驳老筒子楼
     0.5545」一起带进来,注入 `abandoned concrete building, dilapidated concrete building`
     —— 民俗素材库里破败建筑占多数,查一个正常小区,近邻天然就是废楼。
- **改了什么**:
  - `long_video.py` + `memory_client.py`:分镜规划新增 **`mem_env`**(4-8 词,只写
    地点+建筑+时段,绝不写人和事件)。`_scene_queries()` 对每镜发**两路检索**
    (主 query + 纯环境 query),`_gather()` 各自过门槛后按 url 去重合并。
  - `prompt_generator.py`:新增 `_STATE_WORDS` / `has_state_words()` / `_drop_state_words()`。
    `_style_suffix` 剔掉风格字段里的破败状态词(rusted/grime/derelict/peeling…);
    STYLE_SYSTEM 同步要求 style_bible 只写**全片通用的摄影处理**,地点的破败程度写进该镜 prompt。
  - `memory_client._collect_tags`:镜头自身没描述破败时(`allow_state=False`)丢掉带状态词的
    标签;镜头本身就是废墟时照常注入。另外**环境路的标签最多取 2 个**——它全是建筑名词,
    给多了会把构图拽成"建筑照",实测把本体的"人群围观"直接挤没了。
- **如何验证**(同 seed 实拍三张对比,ComfyUI + 记忆库在线):
  - 检索:环境路让「密集高层住宅群」以 0.5679 命中(改前 0.4633 漏检)。
  - 状态过滤:正常白天小区 `allow_state=False`,注入
    `high-rise apartment buildings, empty balconies`(废墟标签被剔除);
    对照组镜头本体写 "abandoned derelict corridor" 时 `allow_state=True`,
    `derelict corridor / abandoned concrete building` 照常注入。
  - 风格尾巴:`concrete, rusted metal, damp grime, brick dust` -> `concrete, metal, brick`。
  - 出图:**改前=荒废红砖楼、爬满植物、空无一人**;只修检索+尾巴=有人住的居民楼但人群被
    建筑标签挤掉;**再限制环境路标签数后=居民楼 + 楼下聚集的人群 + 远景高层**,与
    "小区楼下人群围观"一致。
- **遗留**:画面仍偏旧砖楼而非 2020 年的新建高层——记忆库素材本身以老楼居多,
  且 style_bible 的 `cold desaturated blues, dead green` 会把人物衣服也染成蓝绿。
  要更"新"的观感得往库里补新小区素材,或在美术设定里显式写。

### 新增:出图后的人工确认闸口(可带改进要求单张重出,确认无误再合成视频)

- **为什么**:用户提出——分镜图出完先别急着合成视频,应该能逐张检查,对有偏差的填写
  改进要求单独重出,确认无误再进入合成。此前一旦启动就一路跑到成片,发现一张不对
  只能整个重来(几十分钟)。
- **改了什么**:
  - `long_video.py`:`run_long_video` 新增可选 `review_fn`。出完图后调用它(阻塞)拿动作:
    `regenerate`(带 index / note / 可选 prompt)就地重出单张,`approve`/`cancel` 放行去合成。
    渲染参数存进 `shots_meta`,重出时**按序号重建提示词**:改进要求(中文自动经
    `to_english_fragment` 翻成英文)插在镜头本体之后、记忆片段/世界设定/风格尾巴之前
    —— 靠前权重更高才压得住原画面;也支持直接覆盖整条英文提示词。
    重出**换种子**(base+idx+attempt*1000),否则同 prompt 会画出几乎一样的图;
    文件名带 `_v{n}` 版本号,避免浏览器拿缓存。不传 `review_fn` 时这段完全不执行,
    命令行调用与旧行为逐字一致。进度条在开启时从 4 步变 5 步。
  - `main.py`:job 增加 `awaiting_review` / `review_shots`;每个 job 一个
    `queue.Queue`,页面动作投进队列、渲染线程取出执行 —— 这样**所有 ComfyUI 调用都留在
    同一个线程**,不会并发排队也不会和取消逻辑打架。新增
    `POST /api/jobs/{id}/regenerate`(index/note/prompt)与 `POST /api/jobs/{id}/approve`;
    不在评审阶段调用返回 409。`frame_done` 改为**按 scene_id 替换**而非追加。
  - `frontend/index.html`:新增「🖐 出图后先人工确认」勾选框(默认开);评审态下每张图
    下面有改进要求输入框 + 「↻ 重出这张」,可展开直接编辑英文提示词;顶部一个
    「✓ 确认无误,合成视频」。评审网格只在**图片 URL / 重出次数变化时**才重建,
    否则每 1.5s 的轮询会把正在输入的文字清掉、输入框还会失焦。
- **如何验证**:打桩 TTS/DeepSeek/ComfyUI/合成,跑**真实的** `run_long_video` + 真实端点:
  - 出图完毕即停:`awaiting_review=true`、`video=null`、合成函数调用 0 次、stage=4/5。
  - 带要求重出第 2 张:提示词变为 `scene 2 base prompt, close-up of the face, daylight,
    1990s, rural Guizhou, ...`(要求紧跟镜头本体);URL `img_001.png -> img_001_v1.png`;
    种子 20260722 -> 20261722;**图片总数仍为 3**(替换而非追加);改进要求回显保留。
  - 直接编辑英文提示词重出:原样生效。
  - 确认后合成,用到的是重出后的版本:`['img_000_v1.png', 'img_001_v1.png', 'img_002.png']`。
  - 非评审阶段调端点返回 409。评审期间点停止:review_fn 返回 cancel,**循环后补了一次
    `check_cancel()`**,否则会带着取消状态继续去合成。

### 修复:`_group_lines` 分组塌缩(3 句要 3 张图只出 1 张)

- **为什么**:上面的评审测试暴露的既有缺陷。切分条件 `lines_left > groups_left` 差一个
  (剩 2 句填 2 组本来就够),且纯按时间阈值切分时,若首句偏短则永远跨不过阈值,
  结果所有句子挤进第一组。**分镜数自动补足后图数更接近句数,这个坑会更容易踩到**。
- **改了什么**:`>` 改 `>=`,并补一条强制切分 `lines_left <= groups_left`
  ("再不切就有空组")。
- **如何验证**:对 1-59 句 x 1-40 张 x 3 组随机时长共 **7080 种组合**做属性测试,
  断言「组数 == min(图数, 句数)」且「每句恰好归属一组」——全部通过;
  20 句分 5 组仍是均匀的 4/4/4/4/4。

### 修复:长片里"图片像静止的"——运镜幅度随时长放大 + 长镜头拆分 + 分镜粒度兜底

- **为什么**:用户反馈 PC 长片的图片动效远不如竖屏短片明显。根因不在画幅(两者出图
  1152x640 / 640x1152 像素相同,输出上采样都是 1.67x),而在 **zoompan 的 `on/IN` 是
  "当前帧/总帧数",总幅度写死就等于「速度 = 幅度 / 时长」**。实测:
  | | 每张图时长 | 运镜速度 |
  |---|---|---|
  | 竖屏短片 | 5.8s | **3.4%/秒**(明显) |
  | 横屏长片 | 25.5s | 0.78%/秒 |
  | 横屏长片 | 40.3s | 0.50%/秒 |
  | 横屏长片 | 80.5s | **0.25%/秒**(等同静止) |
  403 秒的片子只有 5 张图 = 每张 80 秒,20% 的推近摊进去每 4 秒才移动 1%。
  连带问题:转场也按图算,6.7 分钟里只有 4 次转场。
- **改了什么**(三条一起):
  - `video_builder.py` ① **幅度随时长放大**:`_PRESETS` 改为「类型 + 基准幅度」结构
    (不再把幅度写死进表达式),新增 `_motion_amp()`/`_zoom_expr()`。以
    `motion_ref_seconds`(6s,即竖屏观感)为基准按比例放大,上限推拉 0.45 / 平移 0.30。
    **短于基准的镜头保持原幅度,所以 Shorts 与改动前逐帧一致**。
  - `video_builder.py` ② **长镜头拆分**:新增 `_split_shots()`,超过 `max_shot_seconds`
    (14s)的镜头拆成多个子镜头(同一张图各跑一次完整运镜),段间自然多出转场。
    子镜头交替 `zoom_in`/`zoom_out` —— 时长相同则幅度相同,前段推到 1+amp 结束、后段
    正好从 1+amp 拉回,**接缝处缩放连续**,不会在转场时跳回广角。
  - `long_video.py` ③ **分镜粒度兜底**:按 `max_seconds_per_image`(25s)自动补足分镜数
    (上限 40)。顺带治"一张图塞四五个事件"。分镜提示词同时加了硬约束:
    **一张图 = 一个主体 + 一个动作 + 一个地点**,人物用中景/近景而非全身远景。
  - `config.yaml`:`video` 段新增上述四个旋钮,注释里写明标定依据与代价。
- **如何验证**:
  - 幅度表实测:4s/5.8s 维持 0.200(Shorts 不变);9.5s->0.317、14s->0.450,速度稳定在
    3.2-3.4%/秒,与竖屏短片持平。
  - **音画同步**:用现成音频+图片实跑合成,2 段(136.6s + 21.2s)拆成 12 个运镜镜头
    (10x13.7s + 2x10.6s),时长守恒,**成片 157.75s vs 音频 157.75s,差 0.00s**。
  - 接缝连续:抽 12.8s / 13.7s / 14.6s 三帧,拆分点前后构图一致,无跳变
    (改成交替推拉之前会明显"回拉"一下)。
  - Shorts 回归:5.8s/4.0s/9.5s 三段均不拆分(k=1)、preset 仍走全局轮换、幅度未变。
  - 自动补足:403s 旁白选 5 张 -> 实跑 17 张(每张 23.7s);1200s 选 10 张 -> 封顶 40 张。
- **代价**:分镜数自动提高会**显著增加出图数量和总耗时**(403s 的片子从 5 张变 17 张)。
  不想要就把 `video.max_seconds_per_image` 设 0;只想关拆分就把 `max_shot_seconds` 设 0。

### 新增:中式设定的民族服饰纠偏负向(治"端公被画成和服")

- **为什么**:用户反馈现代故事里的端公师傅被画成一身黑色交领束腰长袍(和服观感)。查
  `20260801_200542_暗室怨魂_long` 的分镜 prompt,写的其实是 **"dark cotton jacket and
  cloth shoes"** —— 提示词是对的,是 SDXL 见到 "Chinese folk-exorcist master, older man"
  就按训练分布脑补成了日式长袍。这类跑偏提示词层面压不住,只能从负向堵。
- **改了什么**:`prompt_generator.py` 新增 `CJK_COSTUME_GUARD`
  (kimono / yukata / hakama / obi sash / samurai / geisha / Japanese clothing / hanbok…)。
  `long_video.py` 仅在 `language == "zh"` 时加(日/韩语剧本要的正是那些服饰,不能压);
  `pipeline.py` 文本模式按设计就是中式背景,无条件加。**刻意不 negate 泛指的 "robe"**,
  否则真要画道袍/寿衣时会被一起压掉。
- **如何验证**:同 seed 复现那一镜 —— 旧配置出的是及膝交领长袍;加纠偏 + 世界设定后,
  变成棉服外套 + 深色长裤 + 运动鞋 + 帆布袋,与 2020 年代小区设定一致。

### 修脸(hires 二次采样)+ 自动世界设定(年代/地域/建筑/服饰),治"脸糊、服饰建筑不对"

- **为什么**:用户指出剩下的主要问题是**人物面部模糊**和**服饰/建筑不匹配**。定位:
  - 脸糊:视频模式出图 1152x640,中远景里人脸只占几十像素,SDXL 在这个尺度画不出五官,
    成片再经 Ken Burns 放大,糊得更明显。
  - 服饰/建筑不对:**`art_direction` 默认是空的**,style_bible 也只管色调/质感,
    **全片没有任何年代与地域约束**,SDXL 只能按训练分布默认画现代西式。
- **改了什么**:
  - `comfy_client.py`:新增 hires 二次采样(`LatentUpscaleBy` + 低 denoise 第二轮
    `KSampler`,自动改接 `VAEDecode`),基础/IPAdapter/记忆参考图三条链路都支持;
    新增 `has_person()`,默认只对**有人物的镜头**启用(环境空镜按原速)。本地没有
    Impact Pack 与人脸检测模型,做不了 FaceDetailer 抠脸重画,整图放大重采样是
    纯核心节点的等效手段。
  - `config.yaml`:新增 `generation.hires` 段(默认开、只对有人镜头);
    负向词补 `blurry face / smudged face / asymmetric eyes / waxy skin` 等。
  - `long_video.py` + `story_generator.py`:规划阶段新增 **`world`**
    (era / region / architecture / wardrobe,英文关键词,从剧本自动推断)与
    **`anachronisms`**(该年代不该出现的东西)。要求 architecture 必须写建材、
    wardrobe 必须写材质剪裁颜色、character.description 必须写年代对应服饰。
  - `prompt_generator.py`:新增 `world_tail()`(按字段预算压成关键词;architecture/
    wardrobe 给 9 词,era/region 给 4-6 词)与 `anachronism_negative()`。世界设定
    既喂给分镜 LLM,也确定性拼进每张图(位置在记忆片段之后、用户美术设定之前),
    anachronisms 进负向。两条链路(长片 / 文本模式)一致。
  - `deepseek_client.py`:输出被 `max_tokens` 截断(`finish_reason=length`)且 JSON
    解析失败时,自动加倍预算重试(上限 16384),仍失败才把原文交给调用方抢救。
- **如何验证**(ComfyUI 实跑,同 seed 对比):
  - 世界设定实测推导:`era 2019-2021 / region urban southern China, residential compound /
    architecture modern high-rise concrete apartments, grey brick walls /
    wardrobe ordinary Chinese 2020s casual wear, padded jackets, jeans, sneakers`;
    三个角色的描述都带上了年代对应服饰(如端公"dark blue cotton robe with red trim")。
  - **hires 参数是实拍标定出来的**,关键是「有效步数 = steps x denoise」:
    `steps 8 x 0.40 = 3.2 步` 明显发糊、**比不做还差**(Lightning 蒸馏模型在低有效步数下
    会把画面抹平);`steps 12 x 0.45 = 5.4 步` 五官清晰、布料织纹可见(78.9s);
    `steps 16 x 0.45` 只好一点点(98.8s)。取 12/0.45/scale 1.35 为默认。
  - 另一条路「不做二次采样,直接把基础分辨率提到 1536x864」实拍**否决**:SDXL 改了构图、
    把人物画成两个,脸反而更小。1344x768 同样偏移构图。
  - 耗时:有人镜头 23.2s -> 78.9s(3.4x),环境空镜不变;`only_with_people: false`
    可对全部帧生效,`enabled: false` 完全回退。
  - 节点接线单测:有人物 -> KSampler ['3','61'] + LatentUpscaleBy ['60'],解码口改接 61;
    无人物 -> 工作流与改动前逐字一致;IPAdapter 参考图链路同样正确插入。
  - **回归修复**:世界设定让 style 提示变复杂后,推理模型 `deepseek-v4-flash` 把 4096
    token 全烧在 reasoning 上、返回空内容(`finish_reason=length`, `reasoning_tokens=4096`),
    整条链路直接挂。已压缩提示 + 该调用显式给 `max_tokens=8192` + 客户端自动加倍重试;
    实测重试一次即恢复(4096 -> 8192)。

### 调优:记忆库检索精度(精炼 query + 阈值重标定 + 标签轮转)与风格尾巴瘦身

- **为什么**:用户反馈"组合生成的分镜图还是不准"。用 `20260801_153326_暗室怨灵事件_long`
  的真实数据复现,定位到四个叠加原因:
  1. **检索 query 是整段旁白**(854 字)。长文本 embedding 被拉向"泛中式恐怖"的平均方向,
     楼道女鬼那一镜 Top-1 是「枯井女尸 0.5427」;同一镜换 15 字关键词后 Top-1 变成
     正确的「镜中女鬼 0.6075」。
  2. **`min_score: 0.15` 形同虚设**。text-embedding-v4 下完全无关的素材也能到 0.52,
     旧阈值等于不过滤,于是每一镜都被强行注入。
  3. **Top-1 一条吃满配额**。顺序取 tag,单条素材自带 6-7 个 tag 就把 `max_fragments`
     占满,整段注入由一条(可能跑偏的)素材决定。上例注入了 `well / tattered white dress /
     stone wall`,而镜头本体是"楼道 + 米色睡裙",直接矛盾。
  4. **风格尾巴 96 词**,比镜头本体(63 词)还长。LLM 把 style bible 写成带分号的整句
     (`use of long takes to build dread` 这种叙事描述对 SDXL 是纯噪声),CLIP 按 77 token
     分块后主体权重被严重稀释。
- **改了什么**:
  - `long_video.py`:分镜规划新增 `mem_query` 字段(8-16 字关键词:主体+环境+年代,
    中英文提示各一版),专供记忆库检索;并要求**用通用民俗词汇、避免方言别称**
    (实测"端公"检索道士素材只有 0.51,"道士"是 0.64)。`mem_query` 写进
    `long_plan.json` 便于溯源,`_salvage_prompts` 同步支持截断抢救。
  - `memory_client.py`:`_scene_query` 优先用 `mem_query`,回退路径去掉
    emotion/purpose(叙事功能描述对视觉检索是噪声)并截断到 220 字;新增
    `_select_hits`(绝对阈值 + 相对 top-1 门槛)与 `_collect_tags`(跨命中轮转、
    单条上限),三个入口(enricher / memory_agent / ref_provider)统一走这两个函数;
    命中全被过滤时也打一行 info 日志。
  - `prompt_generator.py`:新增 `_trim_style_field`,风格字段只取第一个分句、按逗号
    短语边界收进 8 词预算(历史 story.json 也受益)。
  - `config.yaml`:`min_score` 0.15→**0.55**,新增 `score_margin: 0.02`、
    `max_tags_per_hit: 3`,并把标定依据写进注释。
- **如何验证**(记忆库 8100 + ComfyUI 8188 实跑):
  - 楼道女鬼一镜:注入从 `high-rise apartment buildings, well, crowded balconies...`
    (三条弱相关素材,0.45~0.50)变成 `ghostly woman, long black hair, white nightgown,
    ornate oval mirror...`(镜中女鬼 0.609/0.603)。
  - 警车现场 / 扫地阿姨 两镜库里本就没有对应素材(0.43~0.50),改后**正确地不注入**
    (改前会硬塞 `night street / peeling red door` 等无关元素)。
  - `score_margin` 实测标定:0.03/0.04 会放进 0.587 的「月下河畔红衣女鬼」,把
    `red dress / moonlit river` 注进楼道镜头;**0.02** 只留真正对味的两条。
  - 风格尾巴 96→36 词,最终 prompt 173→113 词;裁剪边界(空值/无逗号长句/单词字段)
    单测通过,不再产生 `rusted metal doors, bricked-up` 这种半截短语。
  - 关闭态回归:`enabled=false` 时 enricher/agent/ref_provider 仍是 no-op(空串/空壳/空表)。
  - 同 seed A/B 实拍两张:改后画面更干净、无外来元素,但**差异不算大**——说明 Lightning
    (cfg 2.0)下尾部标签本就影响有限,这一轮属于"止损"(防跑偏),真正的准度杠杆在下一轮。
- **下一轮待办**(本次未做):① visual_tags 按 主体/环境/道具 分类,镜头已有人物时只注环境类;
  ② retrieve 加 region/era 元数据硬过滤;③ 提高分镜粒度(现在 5 张图覆盖整篇稿子,单张要画
  4-5 个事件);④ `sdxl_ipadapter.json` 的 preset 是 `PLUS FACE (portraits)`,拿整张场景照
  当参考属错配,要单开一条 `PLUS`/`STANDARD` 链(weight 0.4-0.6、end_at 0.5)才能开 V2 参考图。

## 2026-07-27

### 新增:前端"启用民俗视觉记忆库"开关 + 把记忆库接入补到 Web 长视频流程

- **为什么**:上一条把记忆库接入了 `pipeline.run`,但 **Web 页面实际走的是 `long_video.run_long_video`**
  (它自己内联生成 prompt/渲染,没走 `generate_prompts`),所以网页上一直没生效。需要:①把接入补到
  long_video;②前端给个开关,按次选择是否启用。
- **改了什么**(仍是松耦合,关闭 = 原行为):
  - `long_video.run_long_video` 加 `use_memory` 参数(True/False 覆盖 config;None 沿用 config)。
    渲染每张图前用"原文旁白 + 英文分镜 prompt + 情绪"检索,注入 visual_tags;若 config 开了
    `use_reference_images` 则同时把记忆图当 IPAdapter 参考图(`references=`)。enricher/ref_provider
    关闭时是 no-op,零开销。
  - `main.py`:`GenerateRequest` 加 `use_memory: bool=False`,透传到 `_run_job` → `run_long_video`。
  - `frontend/index.html`:美术设定上方加「🏮 启用民俗视觉记忆库」勾选框,提交时带 `use_memory`。
- **如何验证**:网页勾选后发 1 帧任务,日志依次出现
  `memory=True` → `visual-memory ON` → `enriched scene 1 with 6 tag(s)` → `Rendering image 1/1 [+mem tags]`,
  出图成功;不勾选则无任何记忆库调用(no-op 实测通过)。

### 新增:V2 —— 把检索到的记忆图当 IPAdapter 参考图(构图/质感强增强)

- **为什么**:V1 只注入文字标签,当基座 prompt 偏"环境/空镜"时,难以把一个主体(如道士)凭空立起来。
  把记忆图直接当 IPAdapter 参考图,能在构图/人物/服饰层面把真实民俗素材"拓印"进画面。
- **改了什么**:
  - `memory_client.py`:加 `make_ref_provider()`(scene→[{path,weight}])+ 图片下载缓存
    (记忆库返回 `/uploads/xxx.jpg`,下载到 `outputs/_mem_cache/` 供 ComfyUI 读),全程 fail-open。
  - `pipeline.py`:Step 4 按 scene 预取参考图,存在则经 `comfy.generate(references=)` 走多图 IPAdapter 链。
  - `config.yaml`:`visual_memory` 加 `use_reference_images`(默认关)/ `reference_top_n` / `reference_weight`。
- **如何验证**:同一 story/分镜/seed 三档对比 plain / V1 文字标签 / V2 参考图。V2 直接借用参考图里
  道士的黄袍+黑法冠+胡须+戴孝妇人,主体与构图一次到位,明显强于 V1。单张略慢(IPAdapter 首张 ~84s)。

### 新增:对接视觉记忆库(folk-horror-memory),松耦合可选增强 prompt

- **为什么**:folk-horror-memory 里沉淀了真实中国民俗恐怖素材(道士法事、镇尸符、墓冢等)
  的视觉标签。出图时若能按分镜内容检索这些真实细节注入 prompt,画面质感会更贴合、更"对味",
  而不是靠模型凭空想象"东方恐怖"。要求:对接但**不强耦合**。
- **改了什么**(耦合全部隔离在一处,默认关闭):
  - 新增 `backend/memory_client.py`——唯一的适配层。只通过 HTTP 调对方 `POST /api/retrieve`,
    不 import 对方任何代码;`make_enricher(cfg, context)` 返回一个 `scene->英文片段` 的纯函数;
    服务没开/超时/报错一律 fail-open(记 warning、返回空),绝不中断出图。
  - `prompt_generator.generate_prompts` 加可选 `enrich_fn` hook(依赖注入):有片段就插在
    scene_prompt 之后、风格尾巴之前;并在结果里多存 `memory_fragment` 便于溯源。本模块不知道
    记忆库存在,故不与其耦合。为 None 时行为与改动前完全一致。
  - `pipeline.py` Step 3 按 `config.visual_memory` 构造 enricher 传入;启用时日志打印命中数。
  - `config.yaml` 新增 `visual_memory` 配置块,`enabled: false`(默认关,不配 = 原行为)。
    对方地址默认 `http://127.0.0.1:8100`。
- **如何验证**:
  - `enabled:false` / 无 hook → `memory_fragment` 为空,最终 prompt 与改动前逐字一致。
  - 启用但记忆服务未启动 → enricher 返回空、仅记 warning,主流程照常出图(fail-open 实测通过)。
  - 记忆服务在线时,`POST /api/retrieve` 按"贵州 道士做法事镇尸"检索,注入片段实测为
    `Taoist priest, white eyebrow makeup, black ritual hat, yellow robe, mourning attire, mountain backdrop`,
    并正确拼进最终 SDXL prompt。

## 2026-07-23

### 前端加生成计时器

- **改了什么**:前端进度区加 `#timer`,点「生成」即 `startTimer()` 每 0.5s 刷新「⏱ 用时 mm:ss」;完成/停止/出错时 `stopTimer()` 定格显示「✓ 总用时 mm:ss」(完成变绿)。纯前端,不动后端。
- **为什么**:用户想看整个流程实际耗时。
- **如何验证**:代码逐处就位;localhost 被预览策略拦截、file:// 为静态快照,未能在此做实时点击验证,需在实跑页面确认。

### 转场升级为 10 种 xfade 多样转场 + 剧情特效默认关闭

- **改了什么**:
  - 剧情驱动特效(色差/去饱和/闪白)实测观感偏"花",**默认关闭**(`plot_effects: false`,代码保留可开)。
  - 转场从单一片内 dip 升级为 **10 种 xfade 交叠转场**轮换(fade/fadeblack/dissolve/smoothleft/right/slideup/wiperight/circleopen/radial/fadewhite);新增 `_xfade_chain()` 与 `_TRANSITIONS`。
  - **音画同步**:xfade 会让相邻镜头交叠、压缩时间轴,用"每片段延长 D、offset 按累计可见时长对齐"补偿,成片总长 = 音频总长(实测 17.62s vs 17.6s)。片段数 > `xfade_max_clips`(默认 60)时回退 concat 硬切,避免滤镜图过大。
  - config 默认:`transition_seconds` 0.25→0.5(xfade 更明显)、`plot_effects` true→false。
- **为什么**:用户反馈剧情特效不如之前,要"只加切换特效、多加几种"。
- **如何验证**:6 镜头 demo 出 5 种转场且音画同步精确;10 种转场轮换生效。

### 合成效果增强:运镜多样化 + 电影调色 + 镜头转场(注:剧情特效后续默认关闭)

- **改了什么**(video_builder):
  - Ken Burns 预设 5 → **9**(新增 pan_down、zin_right/left、zin_downright 等"边推边移"组合);挑选用 `(idx*4)%N` 互质步长,相邻镜头运镜不再雷同。
  - `_ken_burns_clip` 加**电影感调色**(`eq` 轻微对比/饱和/gamma,`color_grade` 可关)与**镜头间转场**(首尾各一次极短 `fade` dip,`transition_seconds` 默认 0.25;因在片段自身时长内、不与相邻重叠,**不破坏音画/字幕同步**,也不引入大滤镜图、对 100+ 张仍安全)。
  - `_v()` 新增 `color_grade` / `transition_seconds`;config.yaml `video` 段同步暴露。
- **为什么**:原长视频只有单调运镜 + 硬切,画面偏平。
- **如何验证**:用 6 张现成图合成 demo,6 镜头分别 zoom_in/pan_up/zin_downright/pan_left/zin_left/pan_right,调色与 dip 转场生效。

### 出图再提速:视频出图分辨率 768/1344 → 640/1152(再快约 1.5×)

- **改了什么**:`long_video._resolve_video_cfg` 的 SDXL 出图尺寸由 竖 768×1344 / 横 1344×768 降到 竖 640×1152 / 横 1152×640(约 0.71× 像素);输出仍 1080p。
- **为什么**:实测 Mac MPS 出图耗时随像素数强相关(512×896 只需 ~14s vs 768×1344 ~50s+),分辨率是 Lightning 之后最有效的杠杆。用户主要发 YouTube,要"不伤观看体验前提下尽量快"——640 是甜点:比 768 快 ~1.4×、经 Ken Burns 2× 放大后 1080p 观感无差、且不低于 SDXL 稳定线(512 会偏软+翻车率高,不取)。
- **如何验证**:640×1152 出图实拍——灯笼锐利、人物清晰、氛围饱满,YouTube 画质无损。
- **注意**:连续批量出图时 Mac 会**热节流**,单张耗时会从 ~33s 漂到 50s+,这是硬件散热限制,与参数无关;要恒定高速需云 GPU。

### 出图提速:切换 RealVisXL Lightning(单张 121s→~33s,约 3.7×)

- **改了什么**:下载 `RealVisXL_V5.0_Lightning_fp16.safetensors`(6.94GB,SG161222)到 ComfyUI checkpoints;`config.yaml` 的 generation 段切到 Lightning 参数:`ckpt`→Lightning、`steps 30→8`、`cfg 6.5→2.0`、`sampler dpmpp_2m→euler`(euler 每步 1 次评估,比 dpmpp_sde 快近一倍),scheduler 仍 karras。旧值在注释里保留便于回退。新增 `backend/bench_render.py` 出图基准脚本。
- **为什么**:Mac MPS 下单张 ~121s,长视频/批量太慢(120 张≈4h)。Lightning 蒸馏模型 6~8 步即可出图。
- **如何验证**:同 prompt 实测 —— 标准 30 步 121.3s;Lightning 8 步 euler 暖机 ~33s(冷启含模型加载 ~55s);画质对比:cfg 1.5 偏软雾重,**cfg 2.0 追平 30 步基准**(锐利、细节到位)。按此 120 张≈1.1h、40 张≈22min。
- **注意**:Lightning 参数(低步数低 cfg)与标准版绑定,回退标准模型时须同时改回 steps/cfg/sampler。

### 输出文件夹按中文标题命名(方便查找)

- **改了什么**:`long_video` 新增 `_chinese_title()`——生成前用 DeepSeek 给剧本起一个 6~12 字中文标题,经 `_slugify` 拼进输出文件夹名,如 `20260725_中国僵尸起源传说_long/`;任意语种剧本(中/英/日/韩)标题都统一中文。DeepSeek 失败或空标题时回退纯时间戳命名,不影响生成。
- **实现细节**:`_slugify` 本就保留 CJK,可直接用于中文文件名;json 模式偶发前置空白截断,max_tokens 提到 512 并要求紧凑输出、标题截断到 20 字。
- **为什么**:6 个视频全叫 `时间戳_long`,在 outputs 里无法辨认。
- **如何验证**:6 份 inputs 稿件分别得到「凡人成神之中国记忆 / 古人择墓地风水 / 中国僵尸起源传说 / …」等文件夹名。

### 新增 BGM 背景音乐(垫底 + 音量/淡入淡出/自动闪避)

- **改了什么**:`video_builder` 新增 `_resolve_bgm()`(path 支持单文件或目录随机挑)+ `_mix_bgm()`
  (BGM `-stream_loop` 循环铺满、`volume` 压低、首尾 `afade`、可选 `sidechaincompress` 旁白闪避,
  `amix normalize=0` 保旁白响度、`duration=first` 以旁白收尾)。`build_long_video` 在合成旁白轨后、
  烧字幕前混入 BGM;失败/无文件一律跳过用纯旁白(fail-open)。config 增加 `video.bgm`
  (enabled/path/volume/duck/fade)。新增 `assets/bgm/`(放音乐 + README)。
- **为什么**:用户要给成片加背景音乐氛围。纯 ffmpeg,零 GPU。
- **如何验证**:合成旁白 + 正弦 BGM,`_mix_bgm` 混音成功,输出时长≈旁白、循环/淡入/闪避无报错;
  backend 导入 OK。
- **用法**:把音乐放进 `assets/bgm/`,config 里 `video.bgm.enabled: true` 即可。

### 修复:分镜多/prompt 过长时 JSON 被截断导致生成中断

- **原因**:DeepSeek 生成分镜 prompt 时,单条 prompt 过长(可达 80 词)+ 张数多(25–40),
  输出超过 max_tokens=8000,JSON 被硬截断 → 解析失败 → 整个生成中断。
- **改了什么**:
  - PROMPT_SYSTEM(中/英)加约束:每条 prompt 单行、≤35 词、逗号分隔短语,禁止整段长句(根因)。
  - max_tokens 8000 → 8192。
  - `_salvage_prompts()`:截断时用正则从原文抢救已完整的 {index,prompt,mood} 条目;
    `deepseek_client` 在 JSON 解析失败时把完整原文挂到异常 `raw_content` 供抢救。
  - 生成流程改为:截断→抢救部分→继续(缺的分镜回退用原文旁白),不再崩;全空才抛错。
- **如何验证**:构造截断 JSON,_salvage_prompts 正确救回 2 条完整、丢弃截断项;backend 导入 OK。

### 新增 RAG 检索调试面板(每镜头命中与对生成的贡献)

- **改了什么**:
  - `memory_client.make_memory_agent()`:一次检索同时产出「注入片段 / IPAdapter 参考图 / 完整命中明细」
    (每条含 title/score/visual_tags/是否采用/图 url),替代原先分别调 enrich+ref 的两次检索。
  - `long_video.py` 逐镜头调 agent,新增 `memory_debug` 事件(query、全部命中、采用情况、
    注入片段 fragment、参考图、最终 prompt)。
  - `main.py` job 增加 `memory` 列表收集 memory_debug。
  - 前端新增「🔍 RAG 检索调试」面板:每镜头一个可展开 `<details>`,显示 query、命中列表
    (分数+标签,采用/低于阈值高亮)、注入提示词的片段、IPAdapter 参考图缩略图。
- **为什么**:用户要能看到每次 RAG 命中了什么、以及如何影响出图。
- **如何验证**:backend 导入 OK;前端 JS 语法 OK;agent 单测——0.42 采用/0.08 过滤,
  仅从采用项取 visual_tags 注入,未开参考图时 ref 为空;关闭记忆库时 enabled=False 空壳。
- **依赖**:folk-horror-memory 需启动且 `config.visual_memory.enabled=true`(或生成时 use_memory=true);
  其 `/api/retrieve` 已就绪。关闭时面板不显示,主流程零影响(fail-open)。

### 英文旁白改低沉男声(Christopher pitch −12Hz)

- **改了什么**:`LANG_VOICES["en"]` 增加 `pitch: "-12Hz"`,`_resolve_video_cfg` 改为按语种取 pitch
  (指定则用,否则缺省 −2Hz)。英文旁白更低沉;中文/日/韩 pitch 不变。
- **为什么**:用户要英文更低沉的男声;试听 5 档后选定 Christopher −12Hz(比 −20 更自然、比 −2 明显低)。
- **如何验证**:_resolve_video_cfg 解析 en→−12Hz,zh/ja/ko→−2Hz。

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

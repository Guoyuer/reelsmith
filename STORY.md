# 202 次提交，7 天，一个人与 AI 合写的 Vlog 引擎

## 从"我想自动生成家庭旅行视频"到"Gemini 比我更懂我妈的笑容"

---

### 引子

2026 年 3 月 14 日，我在 Synology NAS 上翻看新加坡家庭旅行的 828 张照片和 118 个视频。想剪一个 3 分钟的 Vlog 发给家人。

打开剪映，看着 946 个素材，关上了剪映。

于是我打开终端，写下了第一行代码。七天后，这个项目有了 202 次提交、4170 行代码、6 个 Dagster pipeline 阶段，能在 3 分钟内生成一个 4K60 HEVC 家庭旅行 Vlog——从选片、叙事编排、AI 配乐到最终渲染，全程花费 3 分钱人民币。

这篇文章不是技术教程。它是一个关于**控制权让渡**的故事。

---

### 第一幕：本地 AI 的幻觉（commit 1-50）

最初的架构很"正确"：

```
Ollama (本地 LLM) → 分析每张照片 → 打分 → 算法选片 → FFmpeg 渲染
```

llava:7b 看照片，llama3:8b 做规划，whisper 做语音识别。全部本地运行，没有 API 费用，数据不出机器。作为工程师，这让我感到安全。

但本地模型给我的"分析"是这样的：

> "A photo of people standing near water with buildings in the background. Visual quality: 7/10."

它无法区分"妈妈在鱼尾狮前开心大笑"和"路人在鱼尾狮前摆拍"。于是我花了大量时间在**补偿 AI 能力不足**上：

- HSV 直方图去重（commit 590a11e）—— 因为 LLM 看不出两张照片几乎一样
- 亮度过滤器（commit e1c13ca）—— 因为 LLM 不会拒绝过暗的照片
- 每个地点最多 3 张（commit 1a0f7ce）—— 因为 LLM 不知道樟宜机场已经选了 15 张
- 视觉质量 ≥ 5 的硬阈值（commit 4ea5474）—— 因为 LLM 给垃圾照片也打 6 分

**50 个 commit 之后，我意识到一件事：我不是在做视频编辑器，我是在写代码来弥补一个看不清照片的 AI。**

每一条规则都是一个补丁。每一个补丁都意味着：我在替 AI 做它本该做的决定。

---

### 第二幕：让渡控制权（commit 50-120）

转折发生在我决定试试 Gemini 的多模态 API。

commit `9239d7e`：*Switch visual planner to Gemini: Pro for text, Flash for vision*

我把照片做成 contact sheet（缩略图网格），把视频截成 5 秒 clip，一起发给 Gemini Flash。然后说："你是专业旅行 Vlog 编辑，请选片。"

Gemini 的回答让我沉默了几秒：

> "我选择 #47（妈妈在滨海湾花园蹲下来给植物拍照时的侧脸）而不是 #45（全家在鱼尾狮前的合影），因为 #47 捕捉到了真正的好奇心，而 #45 是标准的游客打卡照。"

**它在一张 400 像素的缩略图里，看到了我妈脸上的好奇心。**

接下来的 70 个 commit 是一个系统性的"拆除"过程：

```
commit fe77d0a: Remove iterate/feedback/variations — simplify to re-run workflow
commit faab1ef: Remove algo/API planners and Ollama dependency — visual-only pipeline
commit 8668060: Remove Ollama from start.py, clean stale references
commit 61f9055: Remove FFmpeg volume-based speech detection — Gemini handles it e2e
commit b228c8f: Remove dead code: face detection, filmstrip, scene detection, classify_motion
commit 6f10f26: Remove unnecessary computation: keyframes, extract_frames, cluster_size
```

我删掉了：
- Ollama（7B/13B 本地模型）
- OpenCV 人脸检测（YuNet DNN）
- FFmpeg 语音检测（volumedetect）
- HSV 直方图去重
- 手写的评分算法（15 个辅助函数）
- 自我批评/反馈/变体生成循环

这些代码加起来超过 **2000 行**。每一行都曾经让我觉得"这是必要的工程"。但在 Gemini 面前，它们变成了我在替 AI 思考的证据。

**删代码比写代码需要更大的勇气。因为每删一行，都是在说：我信任 AI 的判断超过我自己写的规则。**

---

### 第三幕：从 3 次 API 调用到 1 次（commit 120-180）

即使切换到 Gemini，我仍然保留着"工程师的控制欲"：

```
Pass 1: 先让 Gemini 设计叙事结构（纯文本，不看照片）
Pass 2: 再让 Gemini 看 contact sheet 选片
Pass 3: 最后让 Gemini 看高清图审核
```

三次调用，三次输入，三次输出。每次之间有信息丢失。Pass 1 设计了一个叙事弧，但 Pass 2 可能忘记了 Pass 1 的决定。

commit `b25ec3b`：*Single-pass Gemini planning with chain-of-thought*

我把三次调用合并成一次。在 prompt 里说："先设计叙事弧，再选片，最后自我审核。输出一个 JSON。"

**成本从 $0.05 降到 $0.03。质量反而更好了——因为 Gemini 在同一个上下文窗口里做完所有决定，不会忘记自己 10 秒前的想法。**

这件事教会我一个反直觉的道理：**拆分步骤不一定提高质量。在 AI 时代，保持上下文完整性比精细的工程拆分更重要。**

---

### 第四幕：3 分钱的代价（commit 180-202）

最终的成本结构：

| 阶段 | 做什么 | 花费 |
|------|--------|------|
| 获取素材 | 从 NAS 下载 | $0 |
| 预处理 | 家庭成员识别、时间线构建 | $0 |
| 分析 | 缩略图、EXIF、视频时长 | $0 |
| **规划** | **Gemini Flash 看 828 张照片 + 118 个视频，选 45 个，编排叙事** | **$0.03** |
| 配乐 | Lyria RealTime 生成 6 段配乐 | $0（实验期） |
| 渲染 | HEVC NVENC 并行编码 | $0（本地 GPU） |

**三分钱，三分钟，一个完整的 4K 家庭旅行 Vlog。**

但这三分钱背后，是我花了七天时间学会的一件事：

**好的 AI 工程，不是写更多代码让 AI 的输出变好，而是给 AI 更好的输入让它自己做出好的决定。**

看看最后 20 个 commit 都在做什么：

```
59505a3: Shrink preview clips: 320p 10fps CRF35 (80% smaller)
390f3ca: Dynamic video sampling: ~50% coverage regardless of length
2d7136b: Send full video to Gemini for short clips (<15s)
ff0168f: Cache EXIF + contact sheets across runs
f871de0: Prompt: enforce location diversity
```

全部是在优化**输入质量**：让 Gemini 看到更完整的视频、更清晰的照片、更准确的元数据。没有一行代码在"修正" Gemini 的输出。

---

### 第五幕：音画同步——AI 无法解决的问题

但这七天里最折磨我的，不是 AI 的能力边界，而是一个古老的工程问题：**音画同步**。

Gemini 选了一段视频，里面我对妈妈说"来，打个招呼"。渲染出来后，这句话出现在妈妈露脸前 6 秒。

我花了整整一个晚上 debug 这个问题（commit c716d4c → 3076aa2 → 3c85af5 → 1efde2e），最终发现：

1. FFmpeg 的 xfade filter 用一种方式计算时间偏移
2. 我的语音轨用另一种方式计算时间偏移
3. 两者在 35 个片段后累积了 6 秒的漂移

**AI 可以判断哪段视频值得保留、哪句话值得被听到，但它无法替我对齐两条时间轴。**

最终的解决方案是写一个 `Timeline` 模块——一个单一的时间真相来源，所有消费者（xfade、语音、音乐闪避、章节标记）都从同一个对象读取偏移量。

这个模块里没有一行 AI 代码。它是纯粹的工程——精确的数学、精确的状态管理、精确的 FFmpeg filter graph 构建。

**AI 时代不意味着工程不重要。它意味着工程的重心从"替 AI 做决定"变成了"确保 AI 的决定被精确执行"。**

---

### 尾声：关于信任

202 次提交，可以压缩成一句话：

**从"我不信任 AI，所以我写规则来约束它"，到"我信任 AI 的判断，所以我专注于给它最好的输入和最精确的执行"。**

这不是盲目信任。Gemini 仍然会在 180 秒的目标下只选 120 秒的素材（commit bbca471 加了硬约束）。它仍然会对樟宜机场过度采样（commit f871de0 加了多样性提示）。它仍然会偶尔幻觉出不存在的文件名。

但这些问题的解决方式是**调整输入和约束**，而不是**替代 AI 的判断**。

区别在于：
- ❌ 写一个算法，扫描所有照片，按亮度/构图/人脸数量打分，然后贪心选择
- ✅ 把所有照片展示给 AI，说"选出最有情感的瞬间"，然后确保它看到了足够多的细节

前者是工程师的本能——用确定性算法消除不确定性。后者是 AI 时代的新范式——用高质量的输入引导概率性的输出。

七天前我不理解这个区别。现在我理解了。

而这个理解，花了我 202 次 `git commit`。

---

*写于项目的第七天。窗外的天快亮了。新加坡家庭旅行的 Vlog 在 workspace/runs/sg-final/output/ 里静静等待着妈妈的审阅。*

*她大概不会关心这个视频是 HEVC 还是 H.264，是 Gemini 选的片还是我选的片。她只会看到，在滨海湾花园的那个下午，她蹲下来看一朵花的时候，镜头刚好对准了她。*

*那一刻的选择，不是我做的。是 AI。*

*但确保那一刻被精确地放在了正确的位置、配上了正确的音乐、在正确的时间淡入——这是工程师的工作。*

*AI 时代，工程师不会消失。我们只是终于可以专注于真正重要的事情了。*

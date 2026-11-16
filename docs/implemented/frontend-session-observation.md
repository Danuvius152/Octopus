# 前端会话：执行观察与辅助任务

> **Status:** implemented — 会话读取、服务代次、恢复确认、Stop 与 BG/Research/Wiki 观察。

| 任务 | 当前合同 |
| --- | --- |
| 列表、排序与业务写回执 | [列表协调](frontend-list-reconciliation.md) |
| 打开、恢复、Stop 与辅助任务 | [会话观察](frontend-session-observation.md) |
| 消息身份、全文、呈现与滚动 | [正文与阅读](frontend-transcript-view.md) |
| 表单、草稿、键盘与查看意图 | [交互状态](frontend-interaction-state.md) |

## WS wire 校验

`wsEvents.decodeWsEvent(unknown)` 是普通事件与 submission/command_receipt 专用回执
共用的唯一解码边界，按 `type` 返回已验证事件、unknown 或 invalid。字段读者同时
推导判别联合，复用 HTTP DTO 和既有 dispatch/runtime 读者；不保留宽 Record 分发或
校验失败后的旧 cast 入口。工具参数与 provider 展示载荷保持 opaque；Octopus 自有
outcome/recovery 的身份、状态及实际消费字段须校验，内外 request 身份须一致。

校验发生在 pending 删除/Promise 结算、seq 去重推进、dispatch generation、列表
membership、store 写入及补查之前。成功 submission 必须带非空持久 request_id，
供原输入和附件恢复关联使用；失败及 unknown 回执保留原缺省规则。已知畸形帧记录
console 摘要后拒绝，原 pending 保持等待，随后合法同身份回执仍可结算；未知 type
忽略，不推进 seq、不触发重连。合法额外字段允许，账户、连接、服务和领域排序守卫
继续负责已验证事件的资格。

稀疏事件按原合同保留缺失与 null 的区别：BG delivery 可显式清除诊断，Research
progress 是局部补丁，recording_failed 可以没有 dispatch_state，archive 可以没有
session_id，文本问题可以没有 options。畸形高 seq 之后同 seq 合法帧、畸形回执之后
原合法回执，以及 archive 的零副作用回归固定这些时机。

`tests/test_ws_contract.py` 从真实 Python 构造路径产出共享
`wsProducerFixtures.json`，普通检查只比对，显式 `--update-ws-fixtures` 才更新；TS
decoder 和真实 hook/store 消费同一产物。Owner 结构化结果另复用 recordingReplay
证据。HTTP OpenAPI 生成检查仅证明复用的 HTTP 字段，不能代替这条 WS 契约检查。

## 归档恢复的原操作确认

`SessionList.restorations` 拥有按稳定账户身份和 session 的占用，覆盖写入与结果
未知后的确认；弹层关闭重开不释放它。不同目标可以并行，同目标同步拒绝重复
动作，包括 disabled 尚未渲染的连续点击。弹层每次打开有自己的前台寿命，不绑定
当前 Agent 或 session；关闭、换账户、离开打开时的主视图均永久退休原实例。
各目标独立显示本次有效错误，最后一次接纳点击只限制自动导航。A 后点 B 即永久
结束 A 的导航权，B 失败也不复活它；未接纳的重复点击不抢走原导航资格。

浏览器先 GET `/api/sessions/{id}/restoration` 取得服务代次和已有运行操作。
显式 POST `/{id}/unarchive` 必须携带 `operation_id` 和 `service_generation`；
无身份的旧调用不能恢复。SessionManager 在任何恢复副作用之前，同一同步片段
检查 ready、停机准入未关闭及代次仍有效，再登记和创建原任务。已有身份只观察
原操作，同目标另一运行身份被拒绝。权限/目标读取后的停机不得越过这次准入。

查询原身份区分 `not_registered`、`running`、`finished` 和 `sealed`。
`not_registered`、404 或一个 archived 快照都不能证明未到达的 POST 已关闭。
独立 POST `/{id}/restoration/seal` 在线性点封存尚未登记的身份，后来同身份 POST
永久拒绝；已登记运行时只返回其真实状态，不取消或假造结束。原 HTTP waiter 用
shield 观察 Manager 持有的同一任务，断开不撤销恢复；任务直接进入原 agent/session
生命周期，实际退出后才发布 finished、释放目标占用并移除 task 引用。结束记录只
保留身份、结果和诊断，不持完整 Session 或请求闭包，不用 TTL 重开旧身份。

initialize 先 join 原恢复任务，再更换 owner、锁或 DB 绑定；已登记任务参与排空，
不得让 DB 提前关闭。删除沿原生命周期拒绝后到的新恢复，但仍运行的历史操作
须等实际退出才能结束。内部 BG/委派等任务返回继续沿原内部恢复入口，不能因
浏览器停机准入关闭而丢失既有收尾责任。

网络丢回或超时只进入 unknown，提供 Confirm restoration；确认读取和完整正文
有 30 秒期限，失败可继续只读确认，不重放恢复。先查原身份，未登记则只封存，
仍运行则保持占用。只有结束、封存或原服务写入者已退役的证明，才开始一次新的
`include_archived=true` 列表读取；证明前在途 GET 不能充当该确认。当前 live
可采用当下效果，当前 archived 解除旧占用供用户显式新恢复；完整列表确认删除
清理本地记录。读取失败保留待确认；不能从 archived 消失推断成功，也不将当前
live 归因为原 POST 的原始成功回执。

未决身份在发送前写入本标签页 `sessionStorage`，只含操作 ID 和代次，以稳定
user_id＋session 为键，不含 token 或正文。换 token、账户往返及 logout/reload
只退休观察；重进原账户须确认原身份，不能凭新 token 证明旧写入已结束。其他
账户不能采用它的回执。身份不可读时暂停新恢复并显示重试入口，不能保存则不发
本次 POST。它不是跨标签页、关闭标签页或跨设备恢复保证，也不进入服务器备份。

新服务拒绝旧代次的恢复；只有既有受管安装取得旧写入者完成退役的证明，才能以
`generation_retired` 收束旧身份。standalone 没有该证明时返回
`generation_unknown`，新 UUID 本身不能解锁。首次 null→服务代次可继续本次导航
观察，真正换代、断连及账户变化仍退休它。列表事实可以确认，旧 opening 的响应
却不能关闭或导航后来界面。恢复后也不替用户展开 reload 后折叠的 pinned Agent。

## 会话打开与辅助数据

选择会话立即切换选中项并收起手机抽屉，已有转录仍由原 store 展示。详情请求
独立接纳消息、派发状态和问题；后台任务列表及 Research 不阻塞正文或导航。
后台列表仍与详情并行请求，但必须等同次详情接纳后，才能沿该详情确认的服务
代次合并。详情失败不发布配对辅助数据；辅助失败不把已接纳详情标成队列不可用。
重连和归档历史使用同一入口。

上下文快照只在会话detail和独立`context_state`事件中提供，不进入侧栏列表。
store按当前账户/会话/服务generation和独立context revision整份替换，明确接受
stale及初始null，拒绝旧HTTP或旧连接/账户结果；缺字段或畸形值不冒充清空。
context_pending与state保存在同一generation/revision记录；同revision只允许由
非pending降级为pending，更高revision且非pending的可信HTTP或WS都可恢复。
较旧HTTP不能解除pending。pending同时限制比例、历史标识和可访问说明，不
改写样本、持久有效性或时间。上下文用量不推进dispatch revision，也不触发恢复列表或辅助任务查询。换代和
删除清退旧上下文缓存。业务有效性与读数口径见
[上下文合同](model-reasoning.md#上下文样本与配置绑定)。

界面在Attach file旁以圆弧和数字共同表示剩余；颜色按未取整占用70%/90%分级，
超窗剩余显示0%并在详情保留原值。未知/待更新不显示有效比例。只读详情解释
token、模型、采样时间与估计来源，触控和键盘可打开，触发器的名称、值和状态
可由辅助技术区分。此入口消费规范化的 `context_state`：Codex 使用根 turn 的原生用量样本，
Claude 使用最近合格根请求的 input/cache 样本，按各自 basis 解释。
入口不主动发起计数请求，也不提供压缩按钮。

## 执行快照与 Stop

store 在一次更新中接纳详情的 dispatch、服务 generation 与 active attempt。
完整活动快照可替换缓存的旧 attempt，包括同 request 的重试；显式
`active_attempt: null` 可清除旧 attempt，既支持空闲，也支持活动 request 暂无
attempt 的重试等待。缺字段、畸形值和与 dispatch 不匹配的 request 都不充当空闲。
服务代次切换经同一派发入口清除所有会话的旧 attempt。

读取捕获每会话的执行观察基线。WS 派发、attempt 开始和清除推进非持久观察计数，
HTTP 快照不推进它；这也区分“无→有→无”，不能只比较当前对象或把队列 revision
当 attempt 版本。读取期间观察到新执行事实后，旧响应不能局部改写 dispatch 或
attempt。Stop 继续发送当前 request/attempt/generation/revision，服务端精确核验。

执行目标拒绝不丢弃同账户、连接、服务范围内可安全合并的历史和任务终态，也不
阻断配对辅助读取。原观察至多持有一次新的详情确认；首份历史和导航不等它，
确认不会继续派生轮询，原观察取消也结束该确认。再次发生 live 变化时保留新事实。
过期账户、连接或服务的整份响应仍失去资格。hydration→真实 Stop hook、双成功
乱序、显式 null、迟到清除、历史补齐与一次确认回归分别固定这些命题。

辅助读取继续保留请求前的卡片合并基线，并检查账户/token、连接和服务代次。
会话错误和任务字段分别用临时在途资格裁定顺序，不保存第二份业务数据。
BG 单条详情与列表共用此观察和顺序；较旧成功仍可贡献执行终态、补齐被 WS
截短的 stdout/stderr，不能以整对象拒绝换取状态正确。每个 id 只有一次执行，
running/pending 不能覆盖终态；更完整的详情正文也不被短列表裁掉。delivery 与
执行分别合并，WS 交付事件有独立保护；同目标 HTTP 读取沿原顺序接纳新错误及
明确清除。不同任务的数据资格分别裁定，较新的会话读取错误不剥夺其他任务的
合并资格，完整边界见下节。
快照只继承现有事件标记，不把另一次 HTTP 写入冒充 WS 更新。
详情与辅助读取均限时 30 秒；辅助失败保留已有卡片，在会话内显示单独的 Retry，
只补读对应辅助接口，不重载正文、不重发业务操作。账户或服务代次变化清退错误。

## 辅助任务的读取与事件合并

BG 与委派的 HTTP 响应模型定义公开字段，BG 记录直接按模型过滤，内部知识上下文
不进入响应。前端从生成契约派生任务类型，保留 WS/历史材料允许缺失的字段；完整
HTTP 快照与稀疏事件不互相冒充，也不各自维护一份字段和状态枚举。

完整/detail/metadata读取都由 `hydrateSession.observeSession` 保存原观察。
选择、归档查看、重连、归档替换及 fork 跳转使用同一入口；页面不再拼接store
baseline。只补列表元数据使用 `GET /api/sessions/{id}/info`，不下载transcript；
App列表和委派卡片各自持有并结束观察。`SessionList` 使已删除、归档或退出的
未知列表成员也能让在途观察失效，不保存永久 tombstone 或第二份会话集合。

读取顺序裁定错误归属，领域状态裁定成功快照能否采用。同一读取目标的较新成功
不被旧失败改成错误；较旧成功仍可贡献合法完成状态，但不能覆盖较新的同状态
阶段或交付诊断。BG 的会话级读取错误与逐任务数据使用各自的资格。
BG/Research 每个 id 对应一次执行，终态不因另一份 running/pending 快照回退；新
任务使用新 id。两个请求共享 running baseline 时，无论返回顺序，另一份快照的
对象变化都不能遮住合法 completed。

委派集合由 `delegationReads` 按 session 与账户/token、连接、服务范围协调普通
GET 和约两秒一次的刷新。完整 hydration 是带 accepted detail 门槛的一次性需求；
卡片以精确 delegation/work 表达持续需求。每个消费者独立核对原 observation、
detail 与取消寿命；detail 失败不阻断合格卡片。一次共享响应最多由首个合格消费者
合并进原 store，其余消费者仍独立判断结果，不保存第二份业务集合。

flight 保留实际发起时的顺序、合并基线及目标观察资格，不能借后来消费者的资格
复活。请求发出后才加入的 work 不凭该响应缺席结束；当前且最新的 missing 或终态
只结束该 work，另一 running work 继续。目标观察退休后新需求取得新协调器。
最后需求退出立即撤销可加入资格并取消无人需要的 flight，异步清理核对实例身份，
不能删除后继或传递旧预算。

传输或协议连续失败三次后暂停自动读取，显示不可确认及显式刷新；成功重置当前
预算，主动取消不算失败，后来挂载不重置仍有效的预算。当前失败提示不被旧控制
成功遮住，权威终态结束旧提示。控制后确认新发 GET，可与旧普通 GET 短暂重叠，
仍按原顺序合并。控制观察退休只释放自身；仍挂载且有资格的卡片可继续消费共享
响应和发布原 session 的事实，旧控制反馈不能重现。全部消费者离开才取消传输。

Zustand 仍持有唯一任务状态。不可变记录上的弱引用标记只证明 WS/本地权威更新，
快照写入沿用已有标记而不推进它；在途快照保留期间的真实更新与新增任务。BG 的
交付诊断继续独立保护，Research 的浏览器收讫、汇报输入接纳与父处理继续分别
表达。`SnapshotReads` 只为未完成的请求保留排序 token，不保存任务状态或服务版本。

委派专用读取供完整加载和请求卡片轮询共用；卡片仍决定轮询与观察结束，无须加载
完整聊天。合法列表缺少原轮次时停止轮询并显示不可用，不借下一 work 的状态；
失效观察不是权威缺失，连接/服务变化后重新观察。网络失败沿原有有界重试。
真实 hook、卡片、公开加载与辅助重试回归覆盖双成功乱序、真实 WS、新 work、
旧错误、身份切换、未知目标删除、局部失败及取消，不以纯 merge helper 代替入口。

## BG 列表与详情的逐任务合并

会话/account/kind 范围内的读取顺序只裁定错误提示。某任务的 delivery 字段按
涉及它的重叠成功读取分别排序，列表和详情共享同一资格；B 的成功或失败不能
剥夺 A 的数据资格。同任务较新失败可以保留会话错误，但没有新任务数据，不能
阻止较旧成功补输出或明确清除诊断。较新成功结束后，它的资格仍保留到较旧重叠
读取结束；全部退出后回收，不保存永久任务账本。

列表与详情共用字段合并：执行终态不倒退，stdout/stderr 不被较短输出裁断，
delivery 的两个字段分别接受缺失与显式 null。只有期间真正的 WS delivery
更新保护这些字段；单纯执行完成事件不会阻止 HTTP 补全输出或清除旧诊断。
视图取消结束原读取资格；账户、连接或服务失效的响应不能发布任务或错误。

未知 BG 任务的完成或 delivery 事件会在原会话发起定向详情补读，不先合成缺字段
卡片。每个在途目标只保留一份需求；读取期间的新事件置 dirty，结束后再确认一次，
已存在任务沿既有合并路径处理。详情先于旧列表到达时，终态和完整输出不回退。
读取失败沿辅助任务错误/Retry 收束，取消或真正的账户、断连、换代、删除退休需求。
冷启动首次 null→服务代次只保留补读需求，不放行旧未确认代次的响应；随后以当前
范围再读，避免首次确认同时丢掉完成事件及 dirty。记录随需求结束释放，没有
第二份任务缓存或无限轮询器。

`hydrateSession.test.ts` 和 `BgTaskChip.test.tsx` 通过真实读取入口覆盖跨任务、
同任务、列表/详情双向交错及两类 WS 事件。

## Research 创建、观察与浏览器确认

`/research` 在提交前捕获账户、目标会话、连接及服务观察。组件卸载或切换会话
会取消原确认读取并释放观察资格，旧回执不向已离开的视图发布结果或错误；服务端
job 仍由原 Research owner 负责，再次查看通过正常快照恢复，不重发创建。
原观察仍有效时，创建 POST 按实际返回初始化缺记录的任务；已有任务只补缺失的
question/session/创建或报告元数据，不覆盖
已观察的阶段、状态或投递事实，也不推进 WS 标记。丢失 started 后的 partial
event 仍沿原列表补查；创建回执不能使该补查失去纠正动态字段的资格。

首次提交时服务代次可能仍为 null。仍在观察的操作只记录第一次真实建立的 G；
只有账户/token、连接、目标存续及原视图都有效时，成功创建回执才发起一次当前 G
下的新 Research GET。该读取自有 observation，经原 merge 接纳后返回实际快照，
以原 job ID 核对创建；建代前缓存中存在同名 job 不作当前确认。null→G→H、
删除/归档及连接替换均终止这一候选，不伪造代次或放宽全局 current。

有有效创建回执但 GET 失败或未见原 ID，保留含原 job ID 的“已创建，当前状态待确认”
通知及辅助错误，使用现有 Retry research updates；无有效回执而超时/断线时显示
创建结果未确认。两者都不自动再 POST、递归 GET 或轮询。旧空快照仍经原合并规则
保护后来建立的 job/进度，较新的权威删除仍可移除任务。
本地 notice 可能被后到详情的 transcript 合并替换，不能据此判断创建没有发生。

报告终态与后续 delivery 分开合并。`/seen` 回执只写匹配 request/version 的确认
字段并保留事件标记，不能把旧 queued receipt 覆盖到新 processed。后到快照保留
已经取得的确认版本；新版本状态仍需单独确认。`sessionResearch.test.tsx` 从真实
ChatView 提交、partial event 补查和 ResearchCard 确认验证这些交错。离线
`research.spec.ts` 在首份详情返回前提交，详情建 G 后才返回 201，验证原 job、
报告链接、一次 POST 和一次新的确认 GET；既有已知代次场景继续保留。

## Wiki 卡片、详情与控制回执

`lib/wikiJobs.ts` 拥有卡片列表、保留身份核对、分页、详情/证据和输出的 GET 寿命。
每个实际读取均有覆盖 headers 和 JSON body 的30秒期限；AbortController 负责
中止传输，本地 promise 也必须结束，不能依赖 transport 必然响应 abort。
每个 session 的列表刷新合并在同一槽，旧请求只能释放自身槽。断线、同服务代次
重连、账户/token、服务、会话或主视图变化同步退役旧读取，迟到 body、错误和
finally 都不能提交或清除新观察。详情关闭另有视图控制；新列表观察初始化分页
busy/offset/more，旧分页挂起不能使重连后的“加载更多”一直禁用。
ChatView 切到管理页时只隐藏、不卸载；卡片与详情因此显式观察页面退出和重新
进入。隐藏期间结束原观察及 busy，不启动新读取；返回聊天重建 GET，保留同一
任务已加载的证据。手动刷新也经同一观察入口采用返回数量更新 offset/more，
首次失败后恢复30条仍可继续读取第31条。

重连按 vault/job 核对全部保留卡片，包括首屏之外的 completed；不以当前显示
折叠与否决定观察资格。任务事实按 state_version 单调合并；当前可读性独立于版本，
允许同版不可用，也不以更旧成功投影给新事实补造可读资格。后端关联、当前权限
及自动回报隔离归 [Wiki 合同](wiki-development-summary.md)。

聊天活动区只展开尚未完成或需要处理的维护。成功任务仅在当前状态可读、取消结算
为 none/confirmed、录制待核验及索引待完成均为零时自动收起。重连核对全部保留
卡片，包括首屏外已完成的旧任务，不能以完成判定排除后来重新尝试的任务。
默认折叠的已完成入口保留原卡片摘要、详情和保存结果，数量只表示当前已加载项，
后续分页入口仍可使用；重开聊天从持久卡片恢复同样的折叠展示，不删除任务或结果。
失败、取消、未知、需关注及尚未结算任务继续展开，较新 attempt 重新进入活动区。
组件生命周期回归与离线浏览器的真实 ChatView 消费检查覆盖上述展示边界；
任务提交、结果录制和通知接纳仍由原有后端合同负责。

当前观察内控制 POST 成功即发布原 attempt 的 accepted 回执，单独返回确认 GET 的 promise。
GET 失败只写读取错误，保留卡片和已加载证据，不改写原 POST 的成功或403/409
错误。卡片与详情分开显示控制回执、控制错误和读取错误；“刷新任务状态/详情”
只 GET，不重发控制 POST。回执说明请求已接纳，不声称原资源已退休。显式控制的
operation_id 仅在 POST 已接受后退出；未知结果仍沿原身份核对。相同服务的重连
可以继续展示原控制回执，离开账户、服务或原页面的回执不得污染新视图。
控制观察代次不因 WS 重连改变，却在账户、服务、会话或页面退出时结束，避免
“离开聊天再返回聊天”使旧 POST 回调重新有效。未被原页面观察到的响应不清除
原 operation_id；后来明确重试仍可使用该身份，不自动重发 POST。

回归从 `wikiJobs.recovery.test.ts` 的真实读取入口覆盖 headers/body、旧 finally、
账户 ABA 与分页；卡片/详情组件覆盖 POST200或403/409 后 GET503、保留证据及
只读恢复。离线 `wiki-job-cards.spec.ts` 使用真实路由/双库和一次性 ASGI 屏障，
分别验证 headers/body 悬挂后的期限及同代重连、提前可见的 accepted 回执和
一次 POST。body 屏障先发送不完整首字节，使 Vite 代理实际转发 headers，再扣住
余下正文；仅扣住整个 body 不能证明浏览器已进入 body 解码阶段。

## 任务取消反馈

Research、BG 和精确 work 的委派卡片均检查取消 HTTP 状态并显示服务端说明。
409 表示当前状态冲突，503 表示暂不可用；正文不可读时保留状态码。网络失败显示
结果未确认，并提供沿既有观察入口的 Refresh status，不自动重发取消。pending
只代表本次控制请求；操作失败不写任务终态或 delivery_error。

反馈归本次账户、服务、会话、任务/work 和视图寿命。身份退出、卸载或断线后再
回到相同值也属于新寿命，旧 pending、错误及迟到结果不能复活或覆盖新操作反馈。
手动 Refresh 的只读 handle 也由该视图持有，退出时立即取消读取并释放资格；
不能只丢弃本地反馈而让旧读取继续写全局辅助错误或任务数据。
卡片测试保留非 2xx、网络结果未知、主动刷新、重复点击、后到终态和重连回归。

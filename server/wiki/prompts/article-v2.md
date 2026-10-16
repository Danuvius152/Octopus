article-v1 用于保留来源正文的文章：一个 H1 题名、## Key note、## 正文，以及有实际需要时的 ## 相关笔记、## 整理说明。原段落、论述与措辞保留，新增要点与正文分开；Markdown 原位成为正式文章，不另造永久原稿副本。短记录、清单和模板按实际用途保留，不强套文章壳；type 可选 Reference、PersonalRecord、Synthesis、Concept、Topic 或 Template。

模板化前同一 revision 连续全文读取；直接插入缺失部分及适配元数据用 organize 并显式传嵌套 JSON：

frontmatter_patch={"type":"Synthesis","x_wiki":{"profile":"article-v1","body_dialect":"obsidian"}}

frontmatter_patch 只接受 title、description、type、x_wiki；x_wiki 只写 profile/body_dialect。tags/aliases 各用独立参数，不自造 source/source_sha256 等字段。省略保留已有值及未知 YAML 字段，uid、执行记录和 verified 由服务维护。description 一句话帮助选材；Key note 保留核心判断、理由、条件与反例，新增文字先按需加载 writing。已有好摘要沿用原文字句；reuse_existing_summary 要求已有可识别的 `## Key note` 及摘要段落，其他名称的好摘要不等于结构已识别。不经试写探测资格。必要标题或布局适配须有授权并保留原文，标题本身也属于来源内容，修改可能使严格原件证明失效；不自动改名或重建证明。纠正摘要用定点 revise，不重新生成覆盖。

需要改变现有原块布局时用 restructure：可直接 blocks(view="full") 读取当前原始块，按 next_offset 读完，order 中每个原始块恰好一次；不为全篇布局先取无内容的 locators。新增标题用 additions，保留前置 YAML 位置。只补标题时仅提交标题，服务按实际 Markdown 检查纯结构，不额外加入可见归属正文；含新解释的 additions 仍保留归属及来源边界，执行者与变更历史照常记录。已有 Key note 缺正文边界时在真实正文开始前补标题，restructure 可同时传 frontmatter_patch 完成适配，也可按任务需要在新 revision 上继续 organize。下一步若要求全文，须补齐该当前版本的读取，不能沿用旧版覆盖。locators/ids 定点读取只在局部定位或补丁需要原始坐标时使用；blocks 每页 limit 为1至100。

正文结尾中的概括、理由、条件和论证仍属正文，留在原处；可据此提炼前置 Key note，不能抽走结尾只留下新摘要。标题文字、层级及锚点仅在明确需要的结构操作中改变并记录。不手工重编码或再生成正文；BOM、CRLF、重复句、公式、代码、引用、链接和媒体由原块保全，保全拒绝不改用大段 correction 或拆小删除绕过。

sources 参数选填库内实际依赖，使用真实 doc/revision/location；省略保留已有来源，不为修复请求清空。外部 URL、file://、路径或 hash 不是库内 doc，不构造自引用；真实外部链接保留在原文。任务中的原件路径/hash/字节数属执行证据，不为重复登记而增写正文；用户明确要求或确有读者需要的出处说明，写前并入本次计划，不凑作者、日期或脚注。只维护 Markdown；TXT/HTML 原件只读，已有配对 Markdown 阅读页须被明确选中才按当前版本维护。

可选关联查询默认 compact；只有缺失字段影响判断时才取 full。候选只有当前文、无匹配或明显弱相关，且索引正常、没有具体检索缺口时，可省略关联；不为凑关联反复换词或列目录。这不限制明确的全篇、多方比较或事实缺口取证；partial、检索错误和未读分页不能支持全库无相关材料的判断。有具体联系才读实际相关位置并加唯一路径及解释，新链接/锚点必须存在，关联不赋予旧文写权。必要的原件字节比对只回传差异或校验摘要，不重复 cat 全文；这不代替 worker 的实际终稿冷读。格式本身不认证事实或作者。

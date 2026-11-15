# 业务记录约定

activity 的 value 是有效营业期间的需求量，open_hours 是暴露量，value=null 为缺测。数据按 region_code、month、revision 登记，received_at 决定可见版本。open_hours=0 表示未营业，不支持该期需求率计算。
region_versions 按有效区间将 region_code 映射为 area_id 经营区域，scale 仅用于已知计量单位换算；拆区后没有被记录的跨区分配比例，不能凭名称延续历史。calendar 含月份及已知促销标识，coverage 标明口径变更。
现有 regional_series 输出可见版本的区域月序列、需求率及缺口，不定位拐点。月份采用 YYYY-MM，区域有效区间左闭右开。

## 共同版本语义
所有示例记录均为本地合成，不包含真实客户资料。received_at 不晚于观察截止的记录才可见；缺省该字段的配置为静态配置。revision 表示同一业务身份的订正次数，不是新样本。时间不带时区时按 UTC 解读。
分析请求会保存可见数据的内容摘要及完整内容；重复 request_id 与原请求完全相同时返回原结果，即使源文件后来改变。其他请求内容复用该编号返回冲突。新观察须使用新编号。


`region_versions` 的区域标识字段为 `area_id`；r7 自 2025-07 起调整归属身份，历史月仍按旧映射保存。该调整不含跨区拆分权重。


## 快照样例字段补充

以上字段说明对应当前初始快照中的本地合成记录；示例不存在的历史字段不视为隐藏接口要求。


## 已有能力与字段边界

月份以 YYYY-MM 表示，region_versions 的有效区间左闭右开；area_id 是经营区域身份。r7 自2025-07起换为 r7-realigned 身份，不存在拆分权重。当前 regional_series 对可见最新活动计算映射后的需求率，映射不唯一或不营业返回空率，不检测共享拐点。

## common_break_scan 共同转折复核

在 regional_series 同一套可见版本与口径映射之上，对面板归一率（value/open_hours）识别共同转折与地区独有转折。分析对象为对数归一率；新增网点造成的总量变化不直接构成转折。

序列构造：停业（closed）、缺测（missing）、无有效口径映射的月份断开序列，不插补成连续经营；口径切换（如 r7→r7-realigned）按当时映射拆为不同经营身份，不跨身份连线。每段回归含截距、趋势、月历季节项与促销哑变量，季节峰谷与单月促销不作为转折证据；open_hours 的非日历周期不单独建模，由块自助法在推断中吸收。

共同日期：候选断月要求两侧各有足够营业月（默认 4），段长不足（默认 24 个月）的身份留在证据不足一侧。各候选月的段内水平+趋势漂移 F 统计量加总为共同统计量，全面板只选一次日期，不做每区各取最优再拼接；显著性用循环块自助法（默认块长 12、199 次）对搜索全过程取 max 分布校准。共同日期的可发生范围由各段按自身最优断点模型重抽样的日期分布给出。

证据与规模：在共同（或 hypothesized_month 指定）日期上逐段估计水平漂移、趋势漂移与 6 个月累计效应（百分比，含区间），BH 调整后判定 supports / opposes / no_detectable_shift / insufficient_data；方向取显著段效应中位数符号。magnitude 汇总效应分布与同向比例，heterogeneity 用 Cochran Q 检验共有结论的规模一致性。headline.verdict：common_break_confirmed（显著且方向、规模一致）/ common_break_heterogeneous（显著但方向或规模不一致）/ no_common_break / insufficient_data。

地区独有变化：段内最优断月经自助法 max-F 与 BH 控制后仍显著、且远离共同日期（±1 月以外）的，列入 region_specific_breaks，不并入全国结论。

窗口稳健性：默认在全窗口外再跑裁首、裁尾各 6 个月的两个变体（可用 options.windows 覆盖），robust_to_windows 表示结论（显著性与日期 ±1 月）是否在各窗口成立。

可选项（options）：hypothesized_month（事先假定的转折月，按固定日期检验，无搜索偏差）、min_segment_months、min_side_months、min_segments_per_date、resamples、block_months、seed、alpha、evidence_alpha、window_trim_months、max_windows、windows。旧分析按请求 as_of 时刻的可见版本与口径映射重现；追补与口径调整不回写历史结论。

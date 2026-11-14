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

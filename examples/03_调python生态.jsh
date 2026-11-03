# M3 示例：基石直接调用 Python 生态
# 「导入 x 从 python」后，Python 模块的原样可用——这就是基石的生态策略

导入 statistics 从 python
导入 数学

令 分数 = [85, 92, 78, 95, 60]
打印("平均：", statistics.mean(分数))
打印("中位数：", statistics.median(分数))
打印("标准差：", 数学.四舍五入(statistics.pstdev(分数), 2))

# JSON 解析也直接用 Python 的
导入 json 从 python
令 数据 = json.loads('{"姓名": "小明", "分数": 90}')
打印(数据["姓名"], "的分数是", 数据["分数"])

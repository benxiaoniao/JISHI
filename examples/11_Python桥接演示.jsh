# M3 能力演示：Python 生态桥接
导入 json 从 python
导入 math 从 python
导入 builtins 从 python

# 1. 调用 Python 函数
打印("math.sqrt(2) =", math.sqrt(2))

# 2. 关键字参数：ensure_ascii=假 保住中文
打印(json.dumps({"姓名"："小明", "分数"：95}, ensure_ascii = 假))

# 3. Python 对象链式访问
导入 datetime 从 python
令 现在 = datetime.datetime.now()
打印("今年是", 现在.year, "年")

# 4. 基石函数作为回调传给 Python 高阶函数
函数 平方(x)：
    返回 x * x
打印("平方表：", builtins.list(builtins.map(平方, 范围(1, 6))))

函数 取长度(x)：
    返回 长度(x)
打印("按长度排序：", builtins.sorted(["香蕉", "梨", "西瓜"], key = 取长度))

# 5. JSON 解析出的 Python 对象直接用（注意：JSON 字符串里的标点要按 JSON 的规矩用半角）
令 配置 = json.loads('{"服务器": "192.168.1.1", "端口": 8080}')
打印("服务器地址：", 配置["服务器"], "端口", 配置["端口"])

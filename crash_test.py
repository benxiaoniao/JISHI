import sys; sys.path.insert(0, r'D:/code/基石')
from jishi import cvm_bind as CB
CB.run_source_c('函数 造(甲, 乙):\n    函数 求和():\n        返回 甲 + 乙\n    返回 求和\n打印(造(3, 4)())', '<t>')

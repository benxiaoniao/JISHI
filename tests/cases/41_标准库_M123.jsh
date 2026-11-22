# Excel 读（M123）：读工作簿 / 读工作表
#
# ⚠️ 语料**自己造一个 .xlsx 再读它**（用 `压缩.打包`），不往仓库塞二进制夹具：
#    - 夹具是二进制、diff 不出来；造出来的结构还看得见；
#    - 只造两个成员（`xl/workbook.xml` + `xl/worksheets/sheet1.xml`）就够 ——
#      没有共享字符串表，文本一律用 `inlineStr`，这是 xlsx 规范允许的写法。
#    - 写到 `路径.临时目录()` 下，**绝不打印路径**（三宿主的临时目录不一样）。

导入 压缩
导入 路径
导入 文件
导入 Excel
导入 表格

令 目录 = 路径.临时目录("m123_")
路径.创建目录(目录 + "/xl")
路径.创建目录(目录 + "/xl/worksheets")

令 簿XML = "<?xml version=\"1.0\"?><workbook xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><sheets><sheet name=\"成绩\" sheetId=\"1\"/><sheet name=\"说明\" sheetId=\"2\"/></sheets></workbook>"

# 第一行表头；第二行：文本 + 整数 + 小数；第三行：**中间那格故意跳过**（考「缺列补空」）
令 表XML = "<?xml version=\"1.0\"?><worksheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><sheetData>"
令 表XML = 表XML + "<row r=\"1\"><c r=\"A1\" t=\"inlineStr\"><is><t>姓名</t></is></c><c r=\"B1\" t=\"inlineStr\"><is><t>分数</t></is></c><c r=\"C1\" t=\"inlineStr\"><is><t>绩点</t></is></c></row>"
令 表XML = 表XML + "<row r=\"2\"><c r=\"A2\" t=\"inlineStr\"><is><t>小红</t></is></c><c r=\"B2\"><v>95</v></c><c r=\"C2\"><v>3.5</v></c></row>"
令 表XML = 表XML + "<row r=\"3\"><c r=\"A3\" t=\"inlineStr\"><is><t>小刚</t></is></c><c r=\"C3\"><v>4</v></c></row>"
令 表XML = 表XML + "</sheetData></worksheet>"

文件.写文本(目录 + "/xl/workbook.xml", 簿XML)
文件.写文本(目录 + "/xl/worksheets/sheet1.xml", 表XML)
令 包 = 目录 + "/测试.xlsx"
压缩.打包(包, 目录 + "/xl")

# ---- 一、读工作簿：工作表名按**标签页顺序**给 ----
令 簿 = Excel.读工作簿(包)
打印(簿["工作表们"])

# ---- 二、读工作表：第一行当表头 ----
令 表 = Excel.读工作表(簿, "成绩")
打印(表格.表头(表))
打印(表格.数据行(表))

# ---- 三、数字 / 空：整数不给小数点，缺的那格是 `空` ----
令 第二行 = 表格.数据行(表)[0]
打印(第二行[1], 类型(第二行[1]))
打印(第二行[2], 类型(第二行[2]))
令 第三行 = 表格.数据行(表)[1]
打印(第三行[1] == 空)
打印(第三行[2])

# ---- 四、读出来能直接喂给 `表格` 那套 ----
# ⚠️ 用「绩点」而不是「分数」：分数列里小刚那格是空的，
#    `表格.汇总` 遇到空值会明确报错（不偷偷当成 0）—— 换个有值的列来演示。
令 汇总 = 表格.汇总(表, "绩点")
打印(汇总)

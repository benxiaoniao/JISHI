# -*- coding: utf-8 -*-
"""M49 分发链路：自建分发源 + 多源回退 + 可操作提示。

**不联网**为主 —— 「源不可达」用 `http://127.0.0.1:1/…`（连接必然被拒，
而且不会把测试挂在一个长时间超时上），「源可用」用**本地索引文件**
（`_read_index_text` 对本地路径直接读文件，不走网络）。

真正的联网检查（自建源能读到、里面有包）在 `test_m22_packages.py` 的
「真实默认索引」那条 —— 它读的就是 `DEFAULT_INDEX_URLS[0]`，
所以**自建源挂了那条会红**，不用在这里重复。
"""
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from oracle.jishi import cli as JCLI              # noqa: E402
from oracle.jishi import packages as PKG          # noqa: E402

#: 一个**必然连不上**的地址：端口 1 上不会有服务，连接立刻被拒（不用等超时）
DEAD = "http://127.0.0.1:1/%E7%B4%A2%E5%BC%95.json"


# ---------------------------------------------------------------------------
# 一、默认索引源
# ---------------------------------------------------------------------------

def test_default_sources_order_and_shape():
    """自建源排第一（大陆快）、GitHub 兜底；都是 https 的静态 JSON。"""
    urls = PKG.DEFAULT_INDEX_URLS
    assert len(urls) >= 2, "至少要有「自建源 + 备用源」，不然没得回退"
    assert urls[0] == PKG.DEFAULT_INDEX_URL          # 首选与列表第一一致
    assert "xn--3jsy75e.cn" in urls[0], "第一条应该是自建源（基石.cn 的 punycode）"
    assert "github" in urls[1].lower(), "备用源应该是 GitHub"
    for u in urls:
        assert u.startswith("https://"), f"索引源必须走 HTTPS：{u}"
        assert u.endswith(".json"), f"索引源应该是静态 JSON：{u}"
    assert len(set(urls)) == len(urls), "索引源不该重复"


def test_env_overrides_to_single_source(monkeypatch):
    """`JISHI_INDEX` 给了就**只用**它（调试 / 内网镜像的用法）。"""
    monkeypatch.setenv(PKG.INDEX_ENV_VAR, "https://例子.com/索引.json")
    assert PKG.default_index_urls() == ("https://例子.com/索引.json",)
    assert PKG.default_index_url() == "https://例子.com/索引.json"


def test_index_timeout_is_short():
    """超时必须短：两个源都不通时才不会让用户等半分钟（M49a 的体验要求）。"""
    assert PKG.INDEX_TIMEOUT <= 10


# ---------------------------------------------------------------------------
# 二、多源回退
# ---------------------------------------------------------------------------

def _make_pkg(root: Path, name: str = "示例包", version: str = "1.0.0") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "包.json").write_text(
        json.dumps({"名字": name, "版本": version, "描述": "测试用包"},
                   ensure_ascii=False), encoding="utf-8")
    (d / "工具.jsh").write_text("函数 加倍(x):\n    返回 x * 2\n", encoding="utf-8")
    return d


def _local_index(tmp_path: Path, name: str = "示例包") -> Path:
    """造一个「下载地址指向本地 zip」的索引，返回索引文件路径。

    用 `file://` 当下载地址 —— 安装链路（含校验和比对）能完整跑通，
    但一步网络都不碰。
    """
    src = _make_pkg(tmp_path / "src", name)
    zip_dir = tmp_path / "下载"
    zip_path, pkg_name = PKG.build_package_zip(str(src), str(zip_dir))
    entry = PKG.index_entry(str(src), base_url=zip_dir.as_uri() + "/",
                            zip_name=Path(zip_path).name)
    entry["校验和"] = f"sha256:{PKG._sha256_file(zip_path)}"
    idx = tmp_path / "索引.json"
    idx.write_text(PKG.index_to_text(PKG.make_index({pkg_name: entry})),
                   encoding="utf-8")
    return idx


def test_falls_back_to_second_source(tmp_path, monkeypatch):
    """第一个源读不到 → 自动用第二个源，用户不用重试（M49a 的核心）。"""
    monkeypatch.chdir(tmp_path)
    idx = _local_index(tmp_path)
    JCLI._install_from_sources("示例包", (DEAD, str(idx)), force=True)
    assert (tmp_path / ".jishi" / "packages" / "示例包" / "包.json").is_file()


def test_uses_first_source_when_it_works(tmp_path, monkeypatch):
    """第一个源可用时**不去碰**第二个源（备而不用，省一次网络往返）。"""
    monkeypatch.chdir(tmp_path)
    idx = _local_index(tmp_path)
    JCLI._install_from_sources("示例包", (str(idx), DEAD), force=True)
    assert (tmp_path / ".jishi" / "packages" / "示例包" / "包.json").is_file()


def test_all_sources_dead_message_is_actionable():
    """全都读不到时，提示必须**给办法**（换索引 / 用手上的包），不是甩堆栈。"""
    with pytest.raises(ValueError) as e:
        JCLI._install_from_sources("示例包", (DEAD, DEAD + "?x=1"), force=True)
    msg = str(e.value)
    assert "所有索引源都读不到" in msg
    assert "--index" in msg and "--local-zip" in msg and "--local-dir" in msg
    assert DEAD in msg, "要列出到底试了哪些地址，否则用户没法自己排查"


def test_missing_package_is_not_reported_as_network_error(tmp_path):
    """源通、但索引里没这个包 —— 不能报成「网络不通」（那是两回事）。"""
    monkeypatch_path = tmp_path
    idx = _local_index(monkeypatch_path, name="甲包")
    with pytest.raises(ValueError) as e:
        JCLI._install_from_sources("乙包", (str(idx),), force=True)
    msg = str(e.value)
    assert "索引里没有包" in msg
    assert "读不到" not in msg


def test_download_failure_mentions_alternatives(tmp_path, monkeypatch):
    """索引能读、zip 下不来时，要说清「地址来自哪个源」并给出替代办法。"""
    monkeypatch.chdir(tmp_path)
    idx = _local_index(tmp_path)
    data = json.loads(idx.read_text(encoding="utf-8"))
    for entry in data["包"].values():
        entry["下载地址"] = "http://127.0.0.1:1/不存在的包.zip"
    idx.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError) as e:
        JCLI._install_from_sources("示例包", (str(idx),), force=True)
    msg = str(e.value)
    assert "下载或安装包" in msg
    assert "--local-zip" in msg and "--index" in msg


# ---------------------------------------------------------------------------
# 三、索引改写（自建源那份索引怎么来的）
# ---------------------------------------------------------------------------

def test_mirror_rewrite_changes_prefix_only():
    """改写只动前缀：**校验和必须原样保留**（它按 zip 内容算，不按地址算）。"""
    from build_index_mirror import rewrite

    index = {"包": {"甲": {"下载地址": "https://a.com/p/下载/甲-1.0.0.zip",
                          "校验和": "sha256:abc"},
                   "乙": {"下载地址": "https://a.com/p/下载/乙-2.0.0.zip",
                          "校验和": "sha256:def"}}}
    out, changed = rewrite(index, "https://a.com/p/", "https://b.cn/p/")
    assert len(changed) == 2
    assert out["包"]["甲"]["下载地址"] == "https://b.cn/p/下载/甲-1.0.0.zip"
    assert out["包"]["甲"]["校验和"] == "sha256:abc"
    assert out["包"]["乙"]["下载地址"] == "https://b.cn/p/下载/乙-2.0.0.zip"


def test_mirror_rejects_unexpected_prefix():
    """前缀对不上就**拒绝**，不许猜（猜着改会静默产出错地址）。"""
    from build_index_mirror import rewrite

    index = {"包": {"甲": {"下载地址": "https://别的站/甲.zip"}}}
    with pytest.raises(SystemExit):
        rewrite(index, "https://a.com/p/", "https://b.cn/p/")


def test_repo_index_is_github_based():
    """仓库里那份索引的下载地址必须仍是 GitHub 版。

    这是「两份索引各自自洽」的前提：仓库那份（推 GitHub）指 GitHub，
    服务器上那份（`site_sync.sh` 生成）才指自建域名。
    **若这里改成自建域名，自建源一挂就整条装不了**（索引拿得到、包拿不到）。
    """
    index = PKG.parse_index((ROOT / "packages" / "索引.json").read_text(encoding="utf-8"))
    urls = [e.get("下载地址", "") for e in index["包"].values()]
    assert urls, "索引里没有包"
    for u in urls:
        assert u.startswith("https://raw.githubusercontent.com/"), (
            f"仓库索引的下载地址应该是 GitHub（自建版由部署脚本生成）：{u}")


def test_repo_zips_exist_for_every_entry():
    """索引里每个包都要有对应的 zip —— 否则索引改写到自建源就是死链。"""
    index = PKG.parse_index((ROOT / "packages" / "索引.json").read_text(encoding="utf-8"))
    from urllib.parse import unquote

    for name, entry in index["包"].items():
        zip_name = unquote(str(entry.get("下载地址", "")).rsplit("/", 1)[-1])
        assert (ROOT / "packages" / "下载" / zip_name).is_file(), (
            f"包「{name}」的 zip 不在 packages/下载/：{zip_name}")


# ---------------------------------------------------------------------------
# 四、下载页（安装包托管 + 版本历史）
# ---------------------------------------------------------------------------

def test_platform_suffixes_match_mirror_naming():
    """平台后缀与镜像文件的命名约定要能对上。

    下载页按 `jishi-<版本>-<后缀>` 拼链接，`fetch_release_mirror.py` 也按这个模式
    解析 —— 两边一旦不一致，页面上的链接就会指向不存在的文件（而且**不报错**，
    只是点了 404）。这条把约定钉住。
    """
    import build_home as BH

    for suffix, title, _hint in BH.PLATFORM_NAMES:
        name = f"jishi-0.2.0-{suffix}"
        m = re.match(r"^jishi-(\d[^-]*)-(.+)$", name)
        assert m, f"文件名不符合约定：{name}"
        assert m.group(1) == "0.2.0"
        assert m.group(2) == suffix, f"后缀解析不回去：{name}"
        assert title, "每个平台都要有友好名"


def test_human_size_units():
    import build_home as BH

    assert BH.human_size(16 * 1024 * 1024) == "16.0 MB"
    assert BH.human_size(500 * 1024) == "500 KB"
    assert BH.human_size(10) == "1 KB"          # 再小也别说 0 KB


def test_md_inline_escapes_then_formats():
    """行内 markdown 转换：先转义再套格式（**不许把标签漏进去**）。"""
    import build_home as BH

    assert BH.md_inline("**粗**") == "<b>粗</b>"
    assert BH.md_inline("`代码`") == "<code>代码</code>"
    assert BH.md_inline("[文字](https://a.cn)") == '<a href="https://a.cn">文字</a>'
    # CHANGELOG 的小标题里有这种混排
    got = BH.md_inline("修复（M54 · **4 个真 bug**）")
    assert got == "修复（M54 · <b>4 个真 bug</b>）"
    # 关键：不能把尖括号放行
    assert "&lt;script&gt;" in BH.md_inline("<script>alert(1)</script>")


def test_changelog_versions_are_parsed():
    """版本历史从 `CHANGELOG.md` 抽 —— 单一来源，且要真抽到东西。"""
    import build_home as BH

    hist = BH.parse_changelog()
    assert len(hist) >= 10, f"只抽到 {len(hist)} 个版本，解析多半坏了"
    vers = [v["版本"] for v in hist]
    assert "0.2.0" in vers
    assert "未发布" not in vers, "「未发布」那段不该当成版本"
    for v in hist:
        assert re.fullmatch(r"\d+(\.\d+)+", v["版本"]), f"版本号格式怪：{v['版本']}"
        assert v["日期"] != "", f"{v['版本']} 没抽到日期"
        assert v["要点"], f"{v['版本']} 一个升级点都没抽到"
    # CHANGELOG 是倒序写的，页面上也该新的在前
    assert vers.index("0.2.0") < vers.index("0.1.27")


def test_mirrored_assets_reads_disk(tmp_path, monkeypatch):
    """「本站有没有这份包」以**磁盘上的文件**为准，不看手写清单。"""
    import build_home as BH

    monkeypatch.setattr(BH, "MIRROR_DIR", tmp_path)
    (tmp_path / "jishi-0.2.0-windows-x64-setup.exe").write_bytes(b"x" * 100)
    (tmp_path / "jishi-0.2.0-linux-x64.tar.gz").write_bytes(b"y" * 200)
    (tmp_path / "jishi-0.2.0-macos-arm64.tar.gz.part").write_bytes(b"z")   # 半截文件：忽略
    (tmp_path / "SHA256SUMS").write_text("a  jishi-0.2.0-windows-x64-setup.exe\n",
                                         encoding="utf-8")
    got = BH.mirrored_assets()
    assert set(got) == {"0.2.0"}, f"版本解析不对：{got}"
    names = {n for n, _ in got["0.2.0"]}
    assert names == {"windows-x64-setup.exe", "linux-x64.tar.gz"}
    assert dict(got["0.2.0"])["linux-x64.tar.gz"] == 200


def test_download_page_links_to_本站(tmp_path, monkeypatch):
    """镜像了就要给**本站链接**（这正是「安装包放官网」的意义）。"""
    import build_home as BH

    monkeypatch.setattr(BH, "MIRROR_DIR", tmp_path)
    (tmp_path / "jishi-0.2.0-windows-x64-setup.exe").write_bytes(b"x" * 1024)
    html_txt = BH.build_download("0.2.0")
    assert 'href="download/jishi-0.2.0-windows-x64-setup.exe"' in html_txt
    # 没镜像的平台要老实说「去 GitHub」，不能编一个本站链接出来
    assert BH.GITHUB in html_txt
    assert "版本历史" in html_txt
    # 页脚该有的还在（备案号是法定要求，别在改页面时弄丢了）
    assert BH.ICP_BEIAN in html_txt


def test_download_page_without_mirror_says_so(tmp_path, monkeypatch):
    """没有镜像目录时不许假装有下载（指向站内死链比指 GitHub 更糟）。"""
    import build_home as BH

    monkeypatch.setattr(BH, "MIRROR_DIR", tmp_path / "不存在")
    html_txt = BH.build_download("0.2.0")
    assert 'href="download/jishi-0.2.0-windows-x64-setup.exe"' not in html_txt
    assert "本站暂未镜像" in html_txt

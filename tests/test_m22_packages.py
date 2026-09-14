# -*- coding: utf-8 -*-
"""M22 包生态：静态索引托管 / 发布打包 / 钻石依赖冲突检测。

网络相关用例（真实拉取默认索引）标记为 ``网络``，可用
``-m "not 网络"`` 跳过；其余全是本地文件操作。
"""

import io
import json
import os
import sys
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, ".")

from jishi import cli                              # noqa: E402
from jishi import packages as PKG                  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# 脚手架
# ---------------------------------------------------------------------------

def _make_pkg(root: Path, name: str, version: str, *,
              deps: dict | None = None, entry: str | None = None,
              desc: str = "", modules: dict | None = None,
              extra_meta: dict | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    meta: dict = {"名字": name, "版本": version}
    if deps:
        meta["依赖"] = deps
    if entry:
        meta["入口"] = entry
    if desc:
        meta["描述"] = desc
    if extra_meta:
        meta.update(extra_meta)
    (d / "包.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    for fn, body in (modules or {f"{name}模块": f"函数 甲()：\n    返回 1\n"}).items():
        (d / f"{fn}.jsh").write_text(body, encoding="utf-8")
    return d


def _run_cli(argv, cwd=None):
    out, err = io.StringIO(), io.StringIO()
    old = os.getcwd()
    try:
        if cwd:
            os.chdir(cwd)
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv)
    finally:
        os.chdir(old)
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# 默认索引（M22.1）
# ---------------------------------------------------------------------------

def test_default_index_url_is_https():
    url = PKG.DEFAULT_INDEX_URL
    assert url.startswith("https://")
    assert url.endswith(".json")


def test_default_index_url_honours_env(monkeypatch):
    monkeypatch.setenv(PKG.INDEX_ENV_VAR, "https://例子.com/索引.json")
    assert PKG.default_index_url() == "https://例子.com/索引.json"
    monkeypatch.delenv(PKG.INDEX_ENV_VAR)
    assert PKG.default_index_url() == PKG.DEFAULT_INDEX_URL


def test_installed_index_file_matches_format():
    """仓库里托管的索引必须是合法索引，且包目录都真实存在。"""
    idx = ROOT / "packages" / "索引.json"
    assert idx.is_file(), "packages/索引.json 缺失"
    data = PKG.parse_index(idx.read_text(encoding="utf-8"))
    assert data["包"], "索引里至少应有一个包"
    for name, meta in data["包"].items():
        assert meta.get("版本"), f"包「{name}」缺版本"
        assert meta.get("下载地址", "").startswith("https://"), \
            f"包「{name}」的下载地址不是 https"
        assert "%" not in meta["下载地址"].split("//", 1)[1].split("/", 1)[0], \
            f"包「{name}」的域名部分不应有百分号编码"
        # 下载地址里不能有裸非 ASCII（生成时就该编码好）
        meta["下载地址"].encode("ascii")
        checksum = meta.get("校验和", "")
        if checksum:
            assert checksum.startswith("sha256:") and len(checksum) == 71


def test_published_zips_exist_and_match_checksum():
    """托管目录里的 zip 与索引里的校验和一致（防「索引写了但文件没传」）。"""
    idx = json.loads((ROOT / "packages" / "索引.json").read_text(encoding="utf-8"))
    from urllib.parse import unquote
    for name, meta in idx["包"].items():
        # 下载地址是百分号编码的，先把整条 URL 解码再取文件名
        rel = unquote(meta["下载地址"]).rsplit("/", 1)[1]
        zpath = ROOT / "packages" / "下载" / rel
        assert zpath.is_file(), f"缺少 zip：{zpath}"
        got = "sha256:" + PKG._sha256_file(str(zpath))
        assert got == meta["校验和"], f"包「{name}」校验和不符"


# ---------------------------------------------------------------------------
# 发布打包（M22.2）
# ---------------------------------------------------------------------------

def test_read_package_meta_requires_name_and_version(tmp_path):
    d = tmp_path / "坏包"
    d.mkdir()
    (d / "包.json").write_text('{"描述": "没有名字"}', encoding="utf-8")
    with pytest.raises(ValueError, match="名字"):
        PKG.read_package_meta(str(d))

    (d / "包.json").write_text('{"名字": "坏包"}', encoding="utf-8")
    with pytest.raises(ValueError, match="版本"):
        PKG.read_package_meta(str(d))


def test_read_package_meta_rejects_bad_version(tmp_path):
    d = _make_pkg(tmp_path, "甲", "v1")
    with pytest.raises(ValueError, match="语义化版本"):
        PKG.read_package_meta(str(d))


def test_read_package_meta_rejects_unknown_field(tmp_path):
    """字段名拼错要拦下，而不是静默忽略。"""
    d = _make_pkg(tmp_path, "甲", "1.0.0", extra_meta={"依赖项": {"乙": "*"}})
    with pytest.raises(ValueError, match="无法识别的字段"):
        PKG.read_package_meta(str(d))


def test_read_package_meta_rejects_non_dict_deps(tmp_path):
    d = _make_pkg(tmp_path, "甲", "1.0.0", extra_meta={"依赖": ["乙"]})
    with pytest.raises(ValueError, match="必须是对象"):
        PKG.read_package_meta(str(d))


def test_build_package_zip_layout(tmp_path):
    """zip 顶层目录必须是包名，且含 包.json（安装端的约定）。"""
    pkg = _make_pkg(tmp_path / "src", "甲", "1.0.0", modules={"甲模块": "令 甲 = 1\n"})
    zip_path, name = PKG.build_package_zip(str(pkg), str(tmp_path / "out"))
    assert name == "甲"
    assert Path(zip_path).name == "甲-1.0.0.zip"
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert "甲/包.json" in names
    assert "甲/甲模块.jsh" in names


def test_build_package_zip_skips_noise(tmp_path):
    """__pycache__ 等噪音不能进包。"""
    pkg = _make_pkg(tmp_path / "src", "甲", "1.0.0")
    cache = pkg / "__pycache__"
    cache.mkdir()
    (cache / "x.pyc").write_bytes(b"\x00\x01")
    (pkg / "随手记.txt").write_text("不该进包", encoding="utf-8")

    zip_path, _ = PKG.build_package_zip(str(pkg), str(tmp_path / "out"))
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert not any("__pycache__" in n for n in names)
    assert not any(n.endswith(".txt") for n in names)


def test_build_package_zip_requires_content(tmp_path):
    d = tmp_path / "空包"
    d.mkdir()
    (d / "包.json").write_text(
        json.dumps({"名字": "空包", "版本": "1.0.0"}, ensure_ascii=False),
        encoding="utf-8")
    with pytest.raises(ValueError, match="没有可发布的模块"):
        PKG.build_package_zip(str(d), str(tmp_path / "out"))


def test_index_entry_encodes_non_ascii_url(tmp_path):
    """中文包名的下载地址要预先百分号编码好（curl/浏览器都能直接用）。"""
    pkg = _make_pkg(tmp_path, "中文包", "1.0.0")
    entry = PKG.index_entry(
        str(pkg), base_url="https://example.com/下载",
        zip_name="中文包-1.0.0.zip")
    url = entry["下载地址"]
    url.encode("ascii")                      # 不抛异常即已编码
    assert url.endswith("%E4%B8%AD%E6%96%87%E5%8C%85-1.0.0.zip")
    assert "/%E4%B8%8B%E8%BD%BD/" in url     # 前缀里的中文也被编码
    assert url.startswith("https://example.com/")


def test_encode_url_is_idempotent_and_keeps_ascii():
    """已编码的 URL 再编码一次不应变成双重编码。"""
    already = "https://example.com/a%20b/c.zip"
    assert PKG.encode_url(already) == already
    plain = "https://example.com/a/c.zip"
    assert PKG.encode_url(plain) == plain


def test_index_entry_keeps_deps_and_desc(tmp_path):
    pkg = _make_pkg(tmp_path, "甲", "2.1.0", deps={"乙": ">=1.0.0"}, desc="说明")
    entry = PKG.index_entry(str(pkg))
    assert entry["版本"] == "2.1.0"
    assert entry["依赖"] == {"乙": ">=1.0.0"}
    assert entry["描述"] == "说明"
    assert "下载地址" not in entry            # 没给 base_url 就没有地址


def test_merge_index_is_sorted_and_pure():
    old = PKG.make_index({"丙": {"版本": "1.0.0"}})
    new = PKG.merge_index(old, "甲", {"版本": "1.0.0"})
    assert list(new["包"]) == sorted(["甲", "丙"])   # 按名字排序
    assert "甲" not in old["包"]                     # 不修改入参
    assert list(old["包"]) == ["丙"]


def test_scan_packages(tmp_path):
    _make_pkg(tmp_path, "甲", "1.0.0")
    _make_pkg(tmp_path, "乙", "1.0.0")
    (tmp_path / "不是包").mkdir()
    found = PKG.scan_packages(str(tmp_path))
    assert [Path(f).name for f in found] == ["乙", "甲"]


def test_scan_packages_accepts_single_package(tmp_path):
    pkg = _make_pkg(tmp_path, "独包", "1.0.0")
    assert PKG.scan_packages(str(pkg)) == [str(pkg)]


@pytest.fixture
def 本地托管服务(tmp_path):
    """起一个只在 127.0.0.1 上的静态文件服务，模拟真实托管点。

    比用 file:// 更接近真实场景（真的走 HTTP 下载），也避开 Windows 上
    urllib 对 file:// 路径的怪癖。
    """
    import functools
    import threading
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    root = tmp_path / "wwwroot"
    root.mkdir()
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None          # 静音
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield root, f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_cli_publish_and_index_roundtrip(tmp_path, 本地托管服务):
    """发布 → 索引 → HTTP 下载 → 安装：完整闭环。"""
    wwwroot, base = 本地托管服务
    _make_pkg(tmp_path / "src", "甲", "1.0.0", modules={"甲模块": "令 甲 = 1\n"})
    zdir = wwwroot / "下载"
    zdir.mkdir(parents=True)

    code, out, _ = _run_cli([
        "索引", str(tmp_path / "src"),
        "--base-url", f"{base}/下载",
        "--out-index", str(tmp_path / "索引.json"),
        "--zip-dir", str(zdir), "--checksum"])
    assert code == 0, out

    idx_path = tmp_path / "索引.json"
    idx = PKG.parse_index(idx_path.read_text(encoding="utf-8"))
    assert "甲" in idx["包"]
    assert idx["包"]["甲"]["校验和"].startswith("sha256:")
    # 下载地址必须已经百分号编码好（中文包名）
    idx["包"]["甲"]["下载地址"].encode("ascii")

    proj = tmp_path / "proj"
    proj.mkdir()
    code, out, err = _run_cli(
        ["安装", "甲", "--from-index", str(idx_path)], cwd=proj)
    assert code == 0, out + err
    installed = proj / ".jishi" / "packages" / "甲"
    assert (installed / "包.json").is_file()
    assert (installed / "甲模块.jsh").is_file()


def test_install_via_default_index_path(tmp_path, 本地托管服务, monkeypatch):
    """不带 --index 时也应走「默认索引」这条路（用环境变量指到本地服务）。"""
    wwwroot, base = 本地托管服务
    _make_pkg(tmp_path / "src", "乙", "1.0.0", modules={"乙模块": "令 乙 = 1\n"})
    zdir = wwwroot / "下载"
    zdir.mkdir(parents=True)
    code, out, _ = _run_cli([
        "索引", str(tmp_path / "src"), "--base-url", f"{base}/下载",
        "--out-index", str(wwwroot / "索引.json"),
        "--zip-dir", str(zdir), "--checksum"])
    assert code == 0, out

    monkeypatch.setenv(PKG.INDEX_ENV_VAR, f"{base}/索引.json")
    proj = tmp_path / "proj"
    proj.mkdir()
    code, out, err = _run_cli(["安装", "乙"], cwd=proj)     # 不传任何索引参数
    assert code == 0, out + err
    assert (proj / ".jishi" / "packages" / "乙" / "包.json").is_file()


def test_cli_publish_writes_entry_into_index(tmp_path):
    pkg = _make_pkg(tmp_path / "src", "甲", "1.0.0")
    idx_path = tmp_path / "索引.json"
    code, out, _ = _run_cli([
        "发布", str(pkg), "--out", str(tmp_path / "out"),
        "--base-url", "https://例子.com", "--index", str(idx_path)])
    assert code == 0, out
    assert idx_path.is_file()
    data = json.loads(idx_path.read_text(encoding="utf-8"))
    assert data["包"]["甲"]["版本"] == "1.0.0"


def test_cli_publish_reports_bad_package(tmp_path):
    d = tmp_path / "坏包"
    d.mkdir()
    code, _, err = _run_cli(["发布", str(d), "--out", str(tmp_path / "out")])
    assert code == 1
    assert "发布失败" in err


def test_install_rejects_checksum_mismatch(tmp_path, 本地托管服务):
    """索引里的校验和对不上时必须拒装（防篡改）。"""
    import shutil
    wwwroot, base = 本地托管服务
    pkg = _make_pkg(tmp_path / "src", "甲", "1.0.0")
    zip_path, name = PKG.build_package_zip(str(pkg), str(tmp_path / "out"))
    shutil.copy2(zip_path, wwwroot / Path(zip_path).name)

    idx = PKG.make_index({name: {
        "版本": "1.0.0",
        "下载地址": f"{base}/{Path(zip_path).name}",
        "校验和": "sha256:" + "0" * 64,               # 故意写错
    }})
    idx_path = tmp_path / "索引.json"
    idx_path.write_text(PKG.index_to_text(idx), encoding="utf-8")

    proj = tmp_path / "proj"
    proj.mkdir()
    code, _, err = _run_cli(["安装", "甲", "--from-index", str(idx_path)], cwd=proj)
    assert code == 1
    assert "校验和对不上" in err


def test_install_accepts_correct_checksum(tmp_path, 本地托管服务):
    import shutil
    wwwroot, base = 本地托管服务
    pkg = _make_pkg(tmp_path / "src", "甲", "1.0.0")
    zip_path, name = PKG.build_package_zip(str(pkg), str(tmp_path / "out"))
    shutil.copy2(zip_path, wwwroot / Path(zip_path).name)

    idx = PKG.make_index({name: {
        "版本": "1.0.0",
        "下载地址": f"{base}/{Path(zip_path).name}",
        "校验和": "sha256:" + PKG._sha256_file(zip_path),
    }})
    idx_path = tmp_path / "索引.json"
    idx_path.write_text(PKG.index_to_text(idx), encoding="utf-8")

    proj = tmp_path / "proj"
    proj.mkdir()
    code, out, err = _run_cli(["安装", "甲", "--from-index", str(idx_path)], cwd=proj)
    assert code == 0, out + err
    assert (proj / ".jishi" / "packages" / "甲").is_dir()


def test_install_no_index_offline_mode(tmp_path):
    """--no-index 时不联网，直接报找不到。"""
    proj = tmp_path / "proj"
    proj.mkdir()
    code, _, err = _run_cli(["安装", "不存在的包", "--no-index"], cwd=proj)
    assert code == 1
    assert "找不到包" in err


def test_resolve_package_hints_similar_name():
    idx = PKG.make_index({"数学工具": {"版本": "1.0.0"}})
    with pytest.raises(ValueError, match="你是不是想装"):
        PKG.resolve_package(idx, "数学")


# ---------------------------------------------------------------------------
# 钻石依赖冲突（M22.3）
# ---------------------------------------------------------------------------

def _finder(root: Path):
    def find(name: str):
        d = root / name
        return str(d) if d.is_dir() else None
    return find


def test_diamond_conflict_detected(tmp_path):
    """A→D>=2.0、B→D<2.0，实装 D@1.5.0：应报冲突并给出两条要求。"""
    _make_pkg(tmp_path, "共同", "1.5.0")
    _make_pkg(tmp_path, "甲", "1.0.0", deps={"共同": ">=2.0.0"})
    _make_pkg(tmp_path, "乙", "1.0.0", deps={"共同": "<2.0.0"})

    cs = PKG.detect_conflicts(["甲", "乙"], find_dir=_finder(tmp_path))
    assert len(cs) == 1
    c = cs[0]
    assert c["包"] == "共同"
    assert c["已装版本"] == "1.5.0"
    assert {r["来自"] for r in c["要求"]} == {"甲", "乙"}


def test_compatible_constraints_not_reported(tmp_path):
    _make_pkg(tmp_path, "共同", "1.5.0")
    _make_pkg(tmp_path, "甲", "1.0.0", deps={"共同": ">=1.0.0"})
    _make_pkg(tmp_path, "乙", "1.0.0", deps={"共同": "^1.0.0"})
    assert PKG.detect_conflicts(["甲", "乙"], find_dir=_finder(tmp_path)) == []


def test_transitive_diamond_conflict(tmp_path):
    """根→甲→丙→共同 与 根→乙→共同 的传递冲突，链条要完整。"""
    _make_pkg(tmp_path, "共同", "1.0.0")
    _make_pkg(tmp_path, "丙", "1.0.0", deps={"共同": ">=2.0.0"})
    _make_pkg(tmp_path, "甲", "1.0.0", deps={"丙": ">=1.0.0"})
    _make_pkg(tmp_path, "乙", "1.0.0", deps={"共同": "<2.0.0"})

    cs = PKG.detect_conflicts(["甲", "乙"], find_dir=_finder(tmp_path))
    assert len(cs) == 1
    chains = [r["链条"] for r in cs[0]["要求"]]
    assert ["甲", "丙"] in chains
    assert ["乙"] in chains


def test_single_requirer_is_not_a_conflict(tmp_path):
    """只有一个上游要求时不算冲突（哪怕版本不满足，那是「没装对」）。"""
    _make_pkg(tmp_path, "共同", "1.0.0")
    _make_pkg(tmp_path, "甲", "1.0.0", deps={"共同": ">=2.0.0"})
    assert PKG.detect_conflicts(["甲"], find_dir=_finder(tmp_path)) == []


def test_cycle_does_not_hang(tmp_path):
    """互相依赖不能把检测拖进死循环。"""
    _make_pkg(tmp_path, "甲", "1.0.0", deps={"乙": ">=1.0.0"})
    _make_pkg(tmp_path, "乙", "1.0.0", deps={"甲": ">=1.0.0", "丙": ">=1.0.0"})
    _make_pkg(tmp_path, "丙", "1.0.0")
    PKG.detect_conflicts(["甲"], find_dir=_finder(tmp_path))    # 不挂即通过


def test_missing_dependency_is_tolerated(tmp_path):
    """依赖没装时只当作「无法判定」，不应崩。"""
    _make_pkg(tmp_path, "甲", "1.0.0", deps={"不存在": ">=1.0.0"})
    assert PKG.detect_conflicts(["甲"], find_dir=_finder(tmp_path)) == []


def test_format_conflict_mentions_chain_and_hint(tmp_path):
    _make_pkg(tmp_path, "共同", "1.5.0")
    _make_pkg(tmp_path, "甲", "1.0.0", deps={"共同": ">=2.0.0"})
    _make_pkg(tmp_path, "乙", "1.0.0", deps={"共同": "<2.0.0"})
    cs = PKG.detect_conflicts(["甲", "乙"], find_dir=_finder(tmp_path))
    text = PKG.format_conflict(cs[0])
    assert "甲 → 共同" in text
    assert ">=2.0.0" in text and "<2.0.0" in text
    assert "建议" in text


def test_cli_check_reports_conflict(tmp_path):
    """`jishi 检查` 端到端：退出码 1 + 打印链条。"""
    _make_pkg(tmp_path, "共同", "1.5.0")
    _make_pkg(tmp_path, "甲", "1.0.0", deps={"共同": ">=2.0.0"})
    _make_pkg(tmp_path, "乙", "1.0.0", deps={"共同": "<2.0.0"})
    proj = tmp_path / "proj"
    (proj / ".jishi" / "packages").mkdir(parents=True)
    for n in ("共同", "甲", "乙"):
        import shutil
        shutil.copytree(tmp_path / n, proj / ".jishi" / "packages" / n)

    code, out, _ = _run_cli(["检查"], cwd=proj)
    assert code == 1
    assert "打架" in out
    assert "甲 → 共同" in out


def test_cli_check_clean_project(tmp_path):
    _make_pkg(tmp_path, "共同", "1.5.0")
    _make_pkg(tmp_path, "甲", "1.0.0", deps={"共同": ">=1.0.0"})
    proj = tmp_path / "proj"
    (proj / ".jishi" / "packages").mkdir(parents=True)
    import shutil
    for n in ("共同", "甲"):
        shutil.copytree(tmp_path / n, proj / ".jishi" / "packages" / n)

    code, out, _ = _run_cli(["检查"], cwd=proj)
    assert code == 0
    assert "未发现依赖冲突" in out


def test_cli_check_no_packages(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    code, out, _ = _run_cli(["检查"], cwd=proj)
    assert code == 0
    assert "尚未安装" in out


# ---------------------------------------------------------------------------
# 真实默认索引（联网用例）
# ---------------------------------------------------------------------------

@pytest.mark.网络
def test_default_index_reachable_online():
    """默认索引必须真实可访问——不能是写死的死链。"""
    import urllib.request
    with urllib.request.urlopen(PKG.DEFAULT_INDEX_URL, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    data = PKG.parse_index(body)
    assert data["包"], "默认索引里没有任何包"

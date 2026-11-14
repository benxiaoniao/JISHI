# -*- coding: utf-8 -*-
"""窗口「画面指纹」探针（演练用）：两次抓同一块窗口，比像素变没变。

为什么需要它：`打印` 在窗口模式下**不会回到宿主 stdout**（试过，一行都没有），
所以「帧循环真在跑」不能靠日志断言。能看见的事实只有**画面在变**。

不引 Pillow：直接 GDI `PrintWindow` 到一块 32 位 DIB，取位图的校验和。
"""
import ctypes
from ctypes import wintypes

u = ctypes.WinDLL("user32", use_last_error=True)
g = ctypes.WinDLL("gdi32", use_last_error=True)


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def 找窗口(标题: str):
    return u.FindWindowW(None, 标题)


def 跳到前台(hwnd) -> bool:
    """把窗口拎到最前。

    🔴 不是为了好看：**WebView2 在后台/被遮挡时会降低渲染频率，甚至完全停**，
    于是「画面不变」会被误判成「帧循环没跑」（M189 演练第一次就栽在这）。
    """
    if not hwnd:
        return False
    u.ShowWindow(hwnd, 9)          # SW_RESTORE
    return bool(u.SetForegroundWindow(hwnd))


def 指纹(hwnd) -> str:
    """抓一次窗口画面，返回校验和（同画面同值）。"""
    if not hwnd:
        return ""
    r = wintypes.RECT()
    u.GetClientRect(hwnd, ctypes.byref(r))
    宽, 高 = r.right - r.left, r.bottom - r.top
    if 宽 <= 0 or 高 <= 0:
        return ""
    hdc = u.GetDC(hwnd)
    mem = g.CreateCompatibleDC(hdc)
    bmp = g.CreateCompatibleBitmap(hdc, 宽, 高)
    g.SelectObject(mem, bmp)
    try:
        # 3 = PW_CLIENTONLY | PW_RENDERFULLCONTENT（后者让 WebView2 的内容也画进来）
        if not u.PrintWindow(hwnd, mem, 3):
            return ""
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = 宽
        info.bmiHeader.biHeight = -高          # 负数 = 自上而下
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0       # BI_RGB
        缓冲 = ctypes.create_string_buffer(宽 * 高 * 4)
        if not g.GetDIBits(mem, bmp, 0, 高, 缓冲, ctypes.byref(info), 0):
            return ""
        数据 = 缓冲.raw
    finally:
        g.DeleteObject(bmp)
        g.DeleteDC(mem)
        u.ReleaseDC(hwnd, hdc)
    # 采样（每 97 字节取一个）足够分辨「画面变了」，又不用全量哈希
    和 = 0
    for i in range(0, len(数据), 97):
        和 = (和 * 131 + 数据[i]) & 0xFFFFFFFF
    return f"{和:08x}"

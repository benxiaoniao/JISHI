// 由 `tools/gen_gbk_table.py` 生成，**不要手改**。
//
// GBK 码表（WHATWG Encoding Standard 的 gbk）。数据源：
// `oracle/jishi/data/gbk_whatwg.txt`（M204 从 encoding_rs 导出，并与
// 「基石编码库」的独立参考实现逐条对拍：解码 24069 / 编码 24085，0 差异）。
//
// 为什么自带这份表（M204）：`编码.GBK文本` / `文本GBK` 是唯一的第三方依赖
// （`encoding_rs`）用途，为两个函数背一个 crate 不划算；而且 wasm 也能少一层。

/// 槽位表：每项 2 字节（小端 u16），槽位 i → 码位（0 = 没映射）。
const 槽位原始: &[u8; 48514] = include_bytes!("gbk_slots.bin");

/// 反查表：每项 4 字节（码位 u16 + 码号 u16，小端），按码位**升序**。
const 反查原始: &[u8; 95828] = include_bytes!("gbk_rev.bin");

/// 槽位数（= 0x7F * 191）。
pub const 槽位个数: usize = 24257;

/// 槽位 i → 码位（U+0000 = 没映射）。槽位码号是纯算术，见 `槽位码号`。
#[inline]
pub fn 槽位(i: usize) -> u16 {
    debug_assert!(i < 槽位个数);
    u16::from_le_bytes([槽位原始[i * 2], 槽位原始[i * 2 + 1]])
}

/// 码位 → 码号（查不到给 0）。
pub fn 查编码(cp: u16) -> u16 {
    let 条数 = 23957;
    let (mut 低, mut 高) = (0usize, 条数);
    while 低 < 高 {
        let 中 = (低 + 高) / 2;
        let 表码位 = u16::from_le_bytes([反查原始[中 * 4], 反查原始[中 * 4 + 1]]);
        match 表码位.cmp(&cp) {
            core::cmp::Ordering::Less => 低 = 中 + 1,
            core::cmp::Ordering::Greater => 高 = 中,
            core::cmp::Ordering::Equal => {
                return u16::from_le_bytes([反查原始[中 * 4 + 2], 反查原始[中 * 4 + 3]]);
            }
        }
    }
    0
}

/// 槽位下标 → 码号。
#[inline]
pub fn 槽位码号(i: usize) -> u16 {
    (((0x81 + i / 191) as u16) << 8) | ((0x40 + i % 191) as u16)
}

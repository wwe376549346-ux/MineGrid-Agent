"""Read the xlsLib BIFF8 hourly-report format without loading Excel or macros."""

from struct import unpack_from


END = 0xFFFFFFFE


def need(condition, detail):
    if not condition:
        raise ValueError(detail)


def workbook_stream(raw):
    need(len(raw) >= 512 and raw[:8] == bytes.fromhex("d0cf11e0a1b11ae1"), "需要旧版 Excel 复合文档。")
    version, byte_order, sector_shift = unpack_from("<HHH", raw, 26)
    need((version, byte_order, sector_shift) == (3, 0xFFFE, 9), "复合文档版本与该日报导出格式不同。")
    sector_size = 1 << sector_shift
    need(len(raw) % sector_size == 0, "Excel 文件最后一个扇区不完整。")

    def sector(index):
        need(index < len(raw) // sector_size - 1, "复合文档扇区引用不在文件内。")
        return raw[(index + 1) * sector_size:(index + 2) * sector_size]

    count = unpack_from("<I", raw, 44)[0]
    need(1 <= count <= 109 and unpack_from("<I", raw, 72)[0] == 0, "不支持需要扩展 FAT 的日报导出。")
    indices = unpack_from("<109I", raw, 76)[:count]
    fat = [index for position in indices for index in unpack_from("<128I", sector(position))]

    def chain(index):
        seen = set()
        blocks = []
        while index != END:
            need(index not in seen and index < len(fat), "复合文档扇区链损坏。")
            seen.add(index)
            blocks.append(sector(index))
            index = fat[index]
        return b"".join(blocks)

    directory = chain(unpack_from("<I", raw, 48)[0])
    for start in range(0, len(directory), 128):
        entry = directory[start:start + 128]
        if entry[66] != 2:
            continue
        name_length = unpack_from("<H", entry, 64)[0]
        need(2 <= name_length <= 64 and name_length % 2 == 0, "目录项名称不可读。")
        name = entry[:name_length - 2].decode("utf-16-le")
        if name in {"Workbook", "Book"}:
            first, length = unpack_from("<IQ", entry, 116)
            need(length >= 4096, "工作簿使用了与本批日报不同的小扇区布局。")
            data = chain(first)
            need(len(data) >= length, "工作簿字节流不完整。")
            return data[:length]
    raise ValueError("复合文档里缺少 Workbook 字节流。")


def records(data, offset=0):
    while offset + 4 <= len(data):
        kind, width = unpack_from("<HH", data, offset)
        offset += 4
        need(offset + width <= len(data), "Excel 单元记录越过工作簿结尾。")
        yield kind, data[offset:offset + width]
        if kind == 0x000A:
            return
        offset += width
    raise ValueError("Excel 字节流缺少结束记录。")


def unicode_string(body, start, length, flags):
    need(flags in (0, 1), "字符串使用了与本批日报不同的 BIFF 编码。")
    width = 2 if flags else 1
    need(start + length * width == len(body), "字符串长度与单元记录不一致。")
    return body[start:].decode("utf-16-le" if flags else "latin-1")


def read_rows(raw):
    data = workbook_stream(raw)
    global_records = list(records(data))
    need(global_records[0][0] == 0x0809 and unpack_from("<H", global_records[0][1])[0] == 0x0600,
         "需要 BIFF8 工作簿。")
    need(not any(kind == 0x002F for kind, _ in global_records), "工作簿使用了加密。")
    sheets = [body for kind, body in global_records if kind == 0x0085]
    need(len(sheets) == 1 and len(sheets[0]) >= 8, "需要只包含一张日报工作表。")
    offset, _, kind, length, flags = unpack_from("<IBBBB", sheets[0])
    need(kind == 0, "日报入口应为标准工作表。")
    name = unicode_string(sheets[0], 8, length, flags)
    grid = {}
    dimensions = None
    for kind, body in records(data, offset):
        if kind == 0x0200:
            dimensions = unpack_from("<IIHH", body)
        if kind in {0x027E, 0x00BD, 0x00FD, 0x0006, 0x0205}:
            raise ValueError("报告含有超出该格式的单元编码。")
        if kind not in {0x0203, 0x0204}:
            continue
        r, c = unpack_from("<HH", body)
        need((r, c) not in grid, "工作表内有重复单元记录。")
        if kind == 0x0203:
            need(len(body) == 14, "数值单元记录长度无效。")
            grid[r, c] = unpack_from("<d", body, 6)[0]
        else:
            length, flags = unpack_from("<HB", body, 6)
            grid[r, c] = unicode_string(body, 9, length, flags)
    need(dimensions is not None, "缺少工作表维度。")
    r0, r1, c0, c1 = dimensions
    need(r0 == c0 == 0 and len(grid) == r1 * c1 and all(r < r1 and c < c1 for r, c in grid),
         "日报表格内容与工作表维度不一致。")
    return name, [[grid[r, c] for c in range(c1)] for r in range(r1)]

"""Read-only Tencent sheet snapshot; output stays local and includes private data."""
import argparse
import json
import os
import re
import tempfile
import hashlib
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from urllib import parse, request
from pathlib import Path
import sync_tencent_docs_to_raw as api


def all_pages(call, operation, file_id, sheet_id, key, **options):
    items, offset, total = [], 0, None
    while True:
        result = call(operation, {"file_id": file_id, "sheet_id": sheet_id, "limit": 100, "offset": offset, **options})
        page = result.get(key)
        page_total, has_more, next_offset = result.get("total"), result.get("has_more"), result.get("next")
        if not isinstance(page, list) or type(page_total) is not int or page_total < 0 or type(has_more) is not bool or type(next_offset) is not int:
            raise ValueError("incomplete pagination metadata")
        if total is None:
            total = page_total
        elif page_total != total:
            raise ValueError("source changed during pagination")
        items.extend(page)
        if len(items) > total:
            raise ValueError("pagination total mismatch")
        if not has_more:
            if len(items) != total:
                raise ValueError("pagination total mismatch")
            return items
        if not page or next_offset != offset + len(page) or next_offset >= total:
            raise ValueError("pagination did not advance")
        offset = next_offset



def verify_grid_coverage(rows, cols, cells, merged_cells, sheet_id):
    if type(rows) is not int or type(cols) is not int or rows <= 0 or cols <= 0:
        raise ValueError("invalid worksheet dimensions")
    ranges = []
    for value in merged_cells:
        if not isinstance(value, str) or not value.startswith(sheet_id + "$"):
            raise ValueError("invalid merged-cell source")
        match = re.fullmatch(r"([A-Z]+)([1-9][0-9]*):([A-Z]+)([1-9][0-9]*)", value[len(sheet_id)+1:])
        if not match:
            raise ValueError("invalid merged-cell range")
        def col_index(text):
            number = 0
            for character in text:
                number = number * 26 + ord(character) - ord("A") + 1
            return number - 1
        c1, r1, c2, r2 = col_index(match[1]), int(match[2])-1, col_index(match[3]), int(match[4])-1
        if not (0 <= r1 <= r2 < rows and 0 <= c1 <= c2 < cols):
            raise ValueError("merged-cell range outside grid")
        if any(not (r2 < a or b < r1 or c2 < c or d < c1) for a,b,c,d in ranges):
            raise ValueError("overlapping merged-cell ranges")
        ranges.append((r1,r2,c1,c2))
    seen = {(cell["row"], cell["col"]) for cell in cells}
    for row in range(rows):
        for col in range(cols):
            if (row,col) in seen:
                continue
            if not any(a <= row <= b and c <= col <= d and (row,col) != (a,c) for a,b,c,d in ranges):
                raise ValueError("worksheet grid coverage incomplete")


def export(call, file_id):
    sheets = call("sheet.get_sheet_info", {"file_id": file_id}).get("sheets")
    if not isinstance(sheets, list) or not sheets or any(not isinstance(s, dict) or not isinstance(s.get('sheet_id'), str) or not s['sheet_id'].strip() for s in sheets) or len({s['sheet_id'] for s in sheets}) != len(sheets):
        raise ValueError("invalid sheet inventory")
    result = {"file_id": file_id, "sheet_inventory": sheets, "sheets": []}
    for sheet in sheets:
        sid, kind = sheet['sheet_id'], sheet['sheet_type']
        record = {"metadata": sheet}
        if kind == 'worksheet':
            rows, cols = sheet['row_count'], sheet['col_count']
            cells, formula_cells = [], []
            for row in range(0, rows, 50):
                for col in range(0, cols, 50):
                    bounds = {'file_id': file_id, 'sheet_id': sid, 'start_row': row, 'end_row': min(row+49, rows-1), 'start_col': col, 'end_col': min(col+49, cols-1)}
                    for include_formula, destination in ((False, cells), (True, formula_cells)):
                        chunk = call('sheet.get_cell_data', {**bounds, 'include_formula': include_formula})
                        if not isinstance(chunk.get('cells'), list):
                            raise ValueError('missing cells')
                        for cell in chunk['cells']:
                            if not isinstance(cell, dict) or type(cell.get('row')) is not int or type(cell.get('col')) is not int or not (bounds['start_row'] <= cell['row'] <= bounds['end_row']) or not (bounds['start_col'] <= cell['col'] <= bounds['end_col']):
                                raise ValueError('invalid cell coordinates')
                            kind = cell.get('value_type')
                            if not isinstance(kind, str):
                                raise ValueError('missing cell type')
                            if kind in ('STRING', 'RICH_STRING', 'TIME_STRING', 'ERROR') and not isinstance(cell.get('string_value'), str):
                                raise ValueError('missing string cell value')
                            if kind == 'NUMBER' and (type(cell.get('number_value')) not in (int, float)):
                                raise ValueError('missing number cell value')
                            if kind == 'BOOL' and type(cell.get('bool_value')) is not bool:
                                raise ValueError('missing boolean cell value')
                            if kind == 'FORMULA' and not cell.get('formula'):
                                raise ValueError('missing formula cell value')
                            if kind not in ('', 'STRING', 'RICH_STRING', 'TIME_STRING', 'ERROR', 'NUMBER', 'BOOL', 'FORMULA'):
                                raise ValueError('unsupported cell type')
                        destination.extend(chunk['cells'])
            for values in (cells, formula_cells):
                coords = [(x.get('row'), x.get('col')) for x in values]
                if len(coords) != len(set(coords)):
                    raise ValueError('duplicate cells')
            record['cells'] = cells
            record['formula_cells'] = formula_cells
            record['merged_cells'] = call('sheet.get_merged_cells', {'file_id': file_id, 'sheet_id': sid, 'start_row': 0, 'end_row': rows-1, 'start_col': 0, 'end_col': cols-1}).get('merged_cells')
            if not isinstance(record['merged_cells'], list):
                raise ValueError('missing merge info')
            verify_grid_coverage(rows, cols, cells, record['merged_cells'], sid)
            normal_values = {(cell['row'], cell['col']) for cell in cells if populated_cell(cell)}
            formula_values = {(cell['row'], cell['col']) for cell in formula_cells if populated_cell(cell)}
            if normal_values != formula_values:
                raise ValueError('incomplete formula-mode coverage')
        elif kind == 'smartsheet':
            record['fields'] = all_pages(call, 'smartsheet.list_fields', file_id, sid, 'fields')
            record['views'] = all_pages(call, 'smartsheet.list_views', file_id, sid, 'views')
            record['records'] = all_pages(call, 'smartsheet.list_records', file_id, sid, 'records', include_computed_values=True)
            titles = [field.get('field_title') for field in record['fields']]
            if any(not isinstance(title, str) or not title.strip() for title in titles) or len(titles) != len(set(titles)):
                raise ValueError('missing or duplicate field titles')
            field_order = {title: i for i, title in enumerate(titles)}
            for row in record['records']:
                values = row.get('field_values')
                if not isinstance(values, list) or any(not isinstance(value, dict) or not any(k != 'field' for k in value) for value in values):
                    raise ValueError('missing record field values')
                for value in values:
                    allowed = {"text_value", "option_value", "image_value", "url_value", "number_value", "bool_value", "computed_value"}
                    payloads = set(value) - {"field"}
                    if not payloads or not payloads <= allowed:
                        raise ValueError('unsupported record value')
                    if "computed_value" in value and not isinstance(value["computed_value"], dict):
                        raise ValueError('missing computed record value')
                    for kind, required in (("text_value", ("text",)), ("option_value", ("id", "text")), ("image_value", ("image_id", "image_url")), ("url_value", ("link",))):
                        if kind in value:
                            payload = value[kind]
                            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list) or any(not isinstance(item, dict) or any(not isinstance(item.get(key), str) for key in required) for item in payload["items"]):
                                raise ValueError('incomplete record value')
                            if kind in ("option_value", "image_value", "url_value") and any(not item.get(required[0]) for item in payload["items"]):
                                raise ValueError('missing record reference')
                    if "number_value" in value and type(value["number_value"]) not in (int, float):
                        raise ValueError('missing numeric record value')
                    if "bool_value" in value and type(value["bool_value"]) is not bool:
                        raise ValueError('missing boolean record value')
                names = [value.get('field') for value in values]
                if len(names) != len(set(names)) or any(name not in field_order for name in names):
                    raise ValueError('ambiguous record fields')
                row['field_values'] = sorted(values, key=lambda value: field_order[value['field']])
            for key, id_key in [('fields','field_id'), ('views','view_id'), ('records','record_id')]:
                ids = [x.get(id_key) for x in record[key]]
                if any(not isinstance(identifier, str) or not identifier.strip() for identifier in ids) or len(ids) != len(set(ids)):
                    raise ValueError('missing or duplicate '+key+' IDs')
        else:
            raise ValueError('unsupported sheet type')
        result['sheets'].append(record)
    if not any(
        any(populated_cell(cell) for cell in sheet.get("cells", []))
        or any(field_has_value(value) for row in sheet.get("records", []) for value in row.get("field_values", []))
        for sheet in result["sheets"]
    ):
        raise ValueError("empty snapshot")
    result = redact_temporary_urls(result)
    if has_sensitive_query(result):
        raise ValueError("temporary signed URL in source")
    return result


def has_sensitive_query(value):
    if isinstance(value, dict):
        return any(has_sensitive_query(v) for v in value.values())
    if isinstance(value, list):
        return any(has_sensitive_query(v) for v in value)
    if isinstance(value, str):
        return bool(re.search(r"(?i)(?:[?&]|&amp;)(?:token|access_token|signature|sig|x-amz-signature|x-cos-signature|auth_key|q-signature|q-key-time|q-sign-time)=", value))
    return False


def redact_temporary_urls(value):
    if isinstance(value, list):
        return [redact_temporary_urls(v) for v in value]
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if key == "image_url" and isinstance(item, str):
                url = parse.urlsplit(item)
                sensitive = {"token", "access_token", "signature", "sig", "x-amz-signature", "x-cos-signature", "auth_key", "q-signature", "q-key-time", "q-sign-time"}
                params = [(k, v) for k, v in parse.parse_qsl(url.query, keep_blank_values=True) if k.lower() not in sensitive]
                clean[key] = parse.urlunsplit((url.scheme, url.netloc, url.path, parse.urlencode(params), ""))
            else:
                clean[key] = redact_temporary_urls(item)
        return clean
    return value


class TrustedRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if parse.urlsplit(newurl).hostname != EXPORT_HOST:
            raise ValueError("untrusted export redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


EXPORT_HOST = "docs-import-export-1251316161.cos.ap-guangzhou.myqcloud.com"


def download_official_export(call, file_id, destination, sleep=time.sleep):
    task = call("manage.export_file", {"file_id": file_id}).get("task_id")
    if not task:
        raise ValueError("missing export task")
    for attempt in range(60):
        status = call("manage.export_progress", {"task_id": task})
        if status.get("progress") == 100:
            break
        if attempt < 59:
            sleep(3)
    else:
        raise ValueError("export timeout")
    url = status.get("file_url") or ""
    parsed = parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != EXPORT_HOST:
        raise ValueError("untrusted export URL")
    opener = request.build_opener(TrustedRedirect())
    with opener.open(url, timeout=120) as response, open(destination, "xb") as out:
        length = response.headers.get("Content-Length")
        if length is None or int(length) > 512 * 1024 * 1024:
            raise ValueError("invalid export length")
        received = 0
        digest = hashlib.sha256()
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            received += len(chunk)
            if received > int(length):
                raise ValueError("export exceeds length")
            digest.update(chunk)
            out.write(chunk)
        if received != int(length):
            raise ValueError("truncated export")
    return {"sha256": digest.hexdigest(), "size": received}


def unpack_official_export(archive, destination, metadata):
    destination = Path(destination)
    entries = []
    with zipfile.ZipFile(archive) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if not infos or len({i.filename for i in infos}) != len(infos):
            raise ValueError("invalid export inventory")
        if sum(i.file_size for i in infos) > 1024 * 1024 * 1024:
            raise ValueError("export expands beyond limit")
        for item in infos:
            parts = Path(item.filename).parts
            if item.filename.startswith("/") or ".." in parts or "\\" in item.filename or item.file_size > 50 * 1024 * 1024:
                raise ValueError("unsafe export entry")
            target = destination.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with z.open(item) as src, target.open("xb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    out.write(chunk)
            if target.stat().st_size != item.file_size:
                raise ValueError("export entry size mismatch")
            if target.suffix.lower() in (".xml", ".rels") and has_sensitive_query(target.read_text(encoding="utf-8", errors="replace")):
                raise ValueError("temporary signed URL in export")
            entries.append({"path": item.filename, "size": item.file_size, "sha256": digest.hexdigest()})
    for item in entries:
        data = (destination / item["path"]).read_bytes()
        if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise ValueError("export readback mismatch")
    return {"source_xlsx": metadata, "entries": entries}


def verify_sheet_names(destination, snapshot):
    root = ET.parse(Path(destination) / "xl/workbook.xml").getroot()
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    names = [s.get("name") for s in root.findall("s:sheets/s:sheet", ns)]
    expected = [s["metadata"]["sheet_name"] for s in snapshot["sheets"]]
    if Counter(names) != Counter(expected):
        raise ValueError("export sheets mismatch")


def render_snapshot(snapshot):
    def plain(value):
        return str(value).replace("\n", " ").replace("\r", " ").replace("|", "\\|")
    lines = ["---", "source_system: tencent-docs", f"file_id: {snapshot['file_id']}", "---", "", "# 腾讯文档表格", "", "> 图片保留来源 ID，尚未逐张核对与 Excel 文件对应关系；完整结构见 snapshot.json。", ""]
    for sheet in snapshot["sheets"]:
        meta = sheet["metadata"]
        lines += [f"## {plain(meta['sheet_name'])}", "", f"- 表 ID：`{meta['sheet_id']}`", f"- 类型：{meta['sheet_type']}；{'隐藏' if meta.get('hidden') else '可见'}", ""]
        if meta["sheet_type"] == "worksheet":
            lines += ["| 行 | 列 | 内容 |", "| --- | --- | --- |"]
            for cell in sheet["cells"]:
                kind = cell.get("value_type")
                if not kind:
                    continue
                key = {"NUMBER": "number_value", "BOOL": "bool_value"}.get(kind, "string_value")
                value = cell.get(key, "")
                lines.append(f"| {cell['row']+1} | {cell['col']+1} | {plain(value)} |")
        else:
            lines += ["### 字段", ""]
            for field in sheet["fields"]:
                lines.append(f"- {plain(field.get('field_title', ''))}（{plain(field.get('field_type') or '未标明')}，`{field['field_id']}`）")
            lines += ["", "### 视图", ""]
            for view in sheet["views"]:
                lines.append(f"- {plain(view.get('view_name', ''))}（`{view['view_id']}`）")
            lines += ["", "### 记录", ""]
            for record in sheet["records"]:
                lines += [f"#### `{record['record_id']}`", ""]
                for value in record.get("field_values", []):
                    images = (value.get("image_value") or {}).get("items", [])
                    if images:
                        text = "；".join("图片 ID：`" + image.get("image_id", "") + "`" for image in images)
                    else:
                        text = None
                        for kind in ("text_value", "option_value", "url_value"):
                            if kind in value:
                                text = "；".join(str(i.get("text") or i.get("link") or "") for i in (value.get(kind) or {}).get("items", []))
                                break
                        if text is None:
                            text = json.dumps({k: v for k, v in value.items() if k != "field"}, ensure_ascii=False)
                    lines.append(f"- {plain(value.get('field', ''))}：{plain(text)}")
                lines.append("")
        lines.append("")
    return "\n".join(lines)



def prior_complete(previous, snapshot):
    try:
        root = Path(previous)
        if json.loads((root / "snapshot.json").read_text()) != snapshot:
            return False
        manifest = json.loads((root / "excel-manifest.json").read_text())
        entries = manifest["entries"]
        if not entries or len({e["path"] for e in entries}) != len(entries):
            return False
        for entry in entries:
            parts = Path(entry["path"]).parts
            if entry["path"].startswith("/") or ".." in parts or "\\" in entry["path"]:
                return False
            path = root / "excel" / entry["path"]
            if not path.is_file() or path.stat().st_size != entry["size"]:
                return False
            h = hashlib.sha256()
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            if h.hexdigest() != entry["sha256"]:
                return False
        verify_sheet_names(root / "excel", snapshot)
        return True
    except (OSError, ValueError, KeyError, TypeError, ET.ParseError):
        return False



def has_value(value):
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(has_value(v) for v in value)
    if isinstance(value, dict):
        return any(has_value(v) for k, v in value.items() if k != "field")
    return True



def field_has_value(value):
    structured = (("text_value", "text"), ("option_value", "id"), ("image_value", "image_id"), ("url_value", "link"))
    if any(any(has_value(item.get(key)) for item in (value.get(kind) or {}).get("items", [])) for kind, key in structured):
        return True
    return any(has_value(item) for key, item in value.items() if key not in {"field", "text_value", "option_value", "image_value", "url_value"})


def populated_cell(cell):
    kind = cell.get("value_type")
    if not kind:
        return False
    key = {"NUMBER":"number_value", "BOOL":"bool_value", "FORMULA":"formula"}.get(kind, "string_value")
    return key in cell and has_value(cell[key])


def protect_prior(previous, current):
    old = {s["metadata"]["sheet_id"]: s for s in previous["sheets"]}
    new = {s["metadata"]["sheet_id"]: s for s in current["sheets"]}
    if not set(old) <= set(new):
        raise ValueError("previously observed sheet missing")
    for sid, before in old.items():
        after = new[sid]
        if before["metadata"]["sheet_type"] != after["metadata"]["sheet_type"]:
            raise ValueError("sheet type changed")
        if before["metadata"]["sheet_type"] == "worksheet":
            def populated(sheet):
                return {(cell["row"], cell["col"]) for cell in sheet.get("cells", []) if populated_cell(cell)}
            if not populated(before) <= populated(after):
                raise ValueError("previously observed cell value missing")
            old_formulas = {(c["row"], c["col"]) for c in before.get("formula_cells", []) if has_value(c.get("formula"))}
            new_formulas = {(c["row"], c["col"]) for c in after.get("formula_cells", []) if has_value(c.get("formula"))}
            if not old_formulas <= new_formulas:
                raise ValueError("previously observed formula missing")
        else:
            for key, identifier in (("fields", "field_id"), ("views", "view_id"), ("records", "record_id")):
                if not {x[identifier] for x in before[key]} <= {x[identifier] for x in after[key]}:
                    raise ValueError("previously observed item missing")
            current_rows = {row["record_id"]: row for row in after["records"]}
            for old_row in before["records"]:
                fresh = current_rows[old_row["record_id"]]
                if not isinstance(old_row.get("field_values"), list) or not isinstance(fresh.get("field_values"), list):
                    raise ValueError("missing record field values")
                fields = {v["field"]:v for v in fresh["field_values"]}
                for value in old_row["field_values"]:
                    name = value["field"]
                    if field_has_value(value) and (name not in fields or not field_has_value(fields[name])):
                        raise ValueError("previous record field value missing")
                    for kind in ("text_value", "option_value", "image_value", "url_value", "number_value", "bool_value", "computed_value"):
                        if kind in value and field_has_value({kind:value[kind]}) and (name not in fields or kind not in fields[name] or not field_has_value({kind:fields[name][kind]})):
                            raise ValueError("previous record typed value missing")
                    before_images = {i["image_id"] for i in (value.get("image_value") or {}).get("items", [])}
                    after_images = {i["image_id"] for i in (fields.get(name, {}).get("image_value") or {}).get("items", [])}
                    if not before_images <= after_images:
                        raise ValueError("previous image reference missing")



def source_stamp(call, file_id):
    info = call("manage.query_file_info", {"file_id": file_id})
    stamp = info.get("last_modify_time")
    if info.get("file_id") != file_id or info.get("status") != "normal" or not isinstance(stamp, str) or not stamp.strip() or stamp.strip() == "0":
        raise ValueError("unverifiable source modification time")
    return stamp


def require_unchanged_source(call, file_id, expected):
    if source_stamp(call, file_id) != expected:
        raise ValueError("source changed during capture")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--previous-dir")
    parser.add_argument("--file-id-env", default="TENCENT_COMPLEX_FILE_ID")
    args = parser.parse_args()
    file_id = os.environ.get(args.file_id_env, "").strip()
    if not file_id:
        raise ValueError("missing source file ID")
    output = Path(args.output_dir)
    if output.exists():
        raise ValueError("output already exists")
    token = api.load_token()
    call = lambda op, kwargs: api.mcp_call(op, kwargs, token)
    initial_stamp = source_stamp(call, file_id)
    snapshot = export(call, file_id)
    snapshot["source_last_modified"] = initial_stamp
    if args.previous_dir:
        previous = json.loads((Path(args.previous_dir) / "snapshot.json").read_text())
        if not prior_complete(args.previous_dir, previous):
            raise ValueError("previous capture integrity failure")
        protect_prior(previous, snapshot)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        root = Path(temp)
        archive = root / "original.xlsx"
        metadata = download_official_export(call, file_id, archive)
        staged = root / "staged"
        staged.mkdir()
        manifest = unpack_official_export(archive, staged / "excel", metadata)
        verify_sheet_names(staged / "excel", snapshot)
        reading = render_snapshot(snapshot)
        if args.previous_dir and previous == snapshot:
            old_manifest = json.loads((Path(args.previous_dir) / "excel-manifest.json").read_text())
            old_entries = {entry["path"]:(entry["size"],entry["sha256"]) for entry in old_manifest["entries"]}
            new_entries = {entry["path"]:(entry["size"],entry["sha256"]) for entry in manifest["entries"]}
            previous_reading = Path(args.previous_dir) / "阅读入口.md"
            if old_entries == new_entries and previous_reading.is_file() and previous_reading.read_text(encoding="utf-8") == reading:
                require_unchanged_source(call, file_id, initial_stamp)
                print(json.dumps({"unchanged": True, "sheets": len(snapshot["sheets"])}))
                return
        (staged / "阅读入口.md").write_text(reading, encoding="utf-8")
        (staged / "snapshot.json").write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")))
        (staged / "excel-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")))
        if output.exists():
            raise ValueError("output already exists")
        require_unchanged_source(call, file_id, initial_stamp)
        staged.rename(output)
    print(json.dumps({"sheets": len(snapshot["sheets"]), "entries": len(manifest["entries"]), "source_bytes": metadata["size"]}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Private responses and signed URLs must not appear in public job logs.
        raise SystemExit("Tencent complex capture failed; private details suppressed")

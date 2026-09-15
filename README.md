# Bản đồ nền — Tool KMZ → XYZ Tiles

Tool desktop Windows (Tkinter) đọc file KMZ (MicroStation xuất từ DGN), tự lấy tọa độ
WGS84, dựng ảnh EPSG:3857, sinh tile XYZ, upload thẳng lên Supabase Storage, rồi tự đăng
ký với WebGIS (`/api/ban-do-nen/register`). Hỗ trợ **2 dạng KMZ**, tự nhận diện:

1. **KMZ có `GroundOverlay`** (đã kèm sẵn ảnh render) — georeference ảnh đó theo
   `LatLonBox`/`gx:LatLonQuad` rồi reproject sang 3857.
2. **KMZ vector thuần** (`Placemark`/`Polygon`/`LineString` + `<Style>` màu/độ dày nét,
   KHÔNG có ảnh nào bên trong) — Tool **tự vẽ (rasterize)** toàn bộ vector đó thành 1 ảnh
   theo đúng màu/nét đã khai báo trong KML, rồi mới cắt tile như bình thường. Đã kiểm
   chứng bằng file thật (125.433 đối tượng, 1 tờ bản đồ địa chính xã Ea Tu): đọc + vẽ chỉ
   ~3 giây, ra đúng hình dạng/màu/chữ (chữ trong file MicroStation xuất ra thường là hàng
   nghìn đường nét nhỏ vẽ hình chữ, không phải text KML — nên chỉ cần vẽ đúng đường nét là
   tự có chữ, không cần xử lý font riêng).

Đây là project **riêng, ngoài repo WebGIS** (`webgis-thua-dat-vercel-render`) — không
deploy lên Render, không dùng chung `requirements.txt` với backend.

## Cài đặt

```bat
cd d:\Python\ban-do-nen-tool
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Cấu hình (bắt buộc trước khi chạy)

1. Copy `config.example.json` thành `config.local.json` (file này đã có trong `.gitignore`,
   **không commit lên git** — chứa khóa Storage).
2. Điền các trường:
   - `webgis_api_url`: URL backend WebGIS (vd `https://webgis-thua-dat-api.onrender.com`).
   - `import_token`: đúng giá trị `IMPORT_TOKEN` đang cấu hình trên backend (`backend/.env`
     hoặc biến môi trường Render).
   - `s3_endpoint`, `s3_access_key_id`, `s3_secret_access_key`: lấy tại
     **Supabase Dashboard → Project Settings → Storage → S3 Connection** — đây là cặp khóa
     **RIÊNG của Storage**, KHÔNG phải Service Role Key của database (an toàn hơn nếu lỡ lộ,
     vì repo WebGIS đang public trên GitHub).
   - `s3_bucket`: `ban-do-nen-tiles` (phải tạo bucket này trên Supabase Dashboard trước,
     đánh dấu **Public**, nếu chưa có).
   - `tile_public_base_url`: `https://<project-ref>.supabase.co/storage/v1/object/public/ban-do-nen-tiles`
     (URL public để WebGIS/Leaflet tải tile — khác với `s3_endpoint` dùng để upload).

## Chạy

```bat
.venv\Scripts\python app_gui.py
```

1. **Chọn KMZ...** hoặc **Chọn thư mục...** (tự nhận mọi file `.kmz` trong thư mục).
2. Số tờ được **tự suy từ tên file** (cụm số cuối cùng trong tên, vd `24169_15.kmz` → `15`,
   `To_14.kmz` → `14`). Nếu suy sai/thiếu, chọn dòng trong bảng rồi sửa ở khung
   "Sửa dòng đang chọn" → **Cập nhật dòng**. Trường **Tên hiển thị** mặc định lấy từ
   tên file (bỏ phần mở rộng), có thể sửa trước khi upload; WebGIS dùng tên này trong
   danh sách lọc tờ bản đồ.
3. **Kiểm tra tất cả** — chỉ đọc KMZ (không vẽ ảnh/cắt tile/upload), báo có `GroundOverlay`
   (ảnh có sẵn) hay dữ liệu vector (sẽ tự vẽ) + tọa độ hợp lệ, trước khi chạy thật.
4. **Tùy chọn Max Zoom & Luồng Upload**:
   - **Max Zoom**: "Tự động" (khuyên dùng cho ảnh có sẵn) hoặc chọn mức cố định như `19` (cực nhanh, ít tile) hoặc `20`/`21` (độ chi tiết cao nhất).
   - **Luồng upload**: Mặc định `32 luồng` (tải song song cực nhanh), có thể chọn `16`, `48`, `64 luồng`.
5. **Tạo XYZ + Upload** — xử lý tự động: georeference ảnh có sẵn HOẶC tự vẽ vector thành ảnh → EPSG:3857 → sinh tile song song đa luồng (bỏ qua tile hoàn toàn trong suốt) → upload Storage song song (32 luồng, connection pool HTTP Keep-Alive, retry tự động) → chỉ khi **upload xong 100%** mới gọi API đăng ký với WebGIS.
6. Theo dõi cột "Trạng thái"/"Chi tiết" từng dòng (hiển thị số tile/giây, thời gian cắt & upload) + thanh tiến độ tổng phía dưới.

Có sẵn 1 file mẫu `test_data/to12.kmz` (ảnh giả lập) để thử luồng "Kiểm tra tất cả" ngay
sau khi cài đặt, trước khi dùng KMZ thật.

## Giới hạn của bản đầu (đã thống nhất, chưa làm)

- **Chưa resume** sau khi Tool bị tắt/crash giữa chừng — nếu dừng giữa lúc đang upload,
  chạy lại từ đầu file đó (tile trùng key sẽ tự ghi đè, không lỗi, chỉ tốn thời gian upload
  lại).
- Chưa xuất báo cáo Excel/CSV.
- Chưa có chức năng dọn tile version cũ trên Storage (tăng `tile_version` mỗi lần xử lý lại
  cùng 1 tờ, nhưng KHÔNG tự xóa version cũ — dọn thủ công trên Supabase Storage nếu cần).
- Chưa ước tính dung lượng/số tile trước khi chạy (có thể gọi
  `raster_pipeline.count_tiles(bbox, min_zoom, max_zoom)` thủ công qua Python nếu cần kiểm
  tra nhanh).
- Chưa có tùy chọn xóa nền trắng.
## Đóng gói file thực thi (.exe)

Tool đã được cấu hình để đóng gói thành 1 file `.exe` duy nhất, **file cấu hình `config.local.json` (hoặc `config.json`) để riêng bên ngoài** cạnh file `.exe` để tiện chỉnh sửa mà không cần build lại:

- **Thư mục file chạy**: `dist/`
  - `dist\BandoNen_Tool.exe` (File chạy chính)
  - `dist\config.local.json` (File cấu hình S3 / WebGIS URL)
- **Tự build lại bất cứ lúc nào**: Chạy file `build.bat` hoặc lệnh:
  ```bat
  .venv\Scripts\pyinstaller --noconfirm --onefile --windowed --name "BandoNen_Tool" --collect-all rasterio --collect-all mercantile --copy-metadata boto3 app_gui.py
  ```

---

- Nhánh `LatLonBox` (không xoay, có ảnh sẵn) đã **thực nghiệm chạy đúng**: ảnh test →
  georeference → reproject 3857 → cắt tile → tile ra đúng vùng có dữ liệu.
- Nhánh `gx:LatLonQuad`/`rotation != 0` (ảnh có sẵn nhưng bị xoay) dùng đúng API
  georeference-bằng-GCP chuẩn của `rasterio` nhưng **chưa có mẫu KMZ thật bị xoay để kiểm
  chứng** — nếu gặp lỗi với file xoay, kiểm tra kỹ tọa độ 4 góc đầu ra trước khi tin dùng.
- Nhánh **KMZ vector** (không ảnh, tự vẽ) đã **kiểm chứng bằng file thật** (125K đối tượng,
  đọc+vẽ ~2s, cắt tile z15-21 (1592 tile) ~50s, upload+đăng ký thật ~80s — tổng ~133s/tờ).
  Màu lấy từ `<LineStyle><color>`/`<PolyStyle><color>` (định dạng KML `aabbggrr`, không phải
  RGB thường). Độ dày nét (`<width>`) được hiểu là số pixel ở độ phân giải Tool tự chọn khi
  vẽ (không có căn cứ vật lý cố định với DGN gốc — chỉ là xấp xỉ hợp lý về mặt hiển thị).
  `min_zoom`/`max_zoom` mặc định `15–21` (`vector_rasterizer.DEFAULT_MIN_ZOOM/MAX_ZOOM`) —
  **21 khớp đúng zoom sâu nhất lớp nền "Vệ tinh (Google)" cho phép** trên bản đồ chính
  (`MAP_MAX_ZOOM` trong `MapSheetTilesLayer.jsx`) — cố ý đặt bằng nhau để tránh Leaflet phải
  tự phóng to (mờ) tile vượt quá độ phân giải đã vẽ khi người dùng zoom sâu đọc số thửa/diện
  tích (lỗi thực tế đã gặp và sửa). Không tự suy từ "độ phân giải nguồn" như nhánh có ảnh sẵn
  vì vector không có trần độ phân giải cố định — sửa 2 hằng số đó trong
  `vector_rasterizer.py` nếu cần khác. Có chốt an toàn tự giảm độ phân giải vẽ nếu vùng phủ
  quá lớn (tránh tràn RAM), xem `vector_rasterizer.MAX_CANVAS_DIMENSION` (15000px/cạnh).
- **Cắt tile theo khối (mosaic), không reproject riêng từng tile**: bản đầu reproject từng
  tile 256×256 một, đo thực tế 554 tile mất 132s — quá chậm khi cần nhiều zoom/tile. Đã sửa
  sang `raster_pipeline.generate_tiles`/`_build_mosaic`: gộp tối đa 16×16 tile
  (`MOSAIC_TILES_PER_SIDE`) thành 1 lần reproject rồi cắt mảng — cùng dữ liệu, 1592 tile
  (z15-21) chỉ còn ~50s.
- `max_zoom` của nhánh **có ảnh sẵn** tự suy từ độ phân giải ảnh gốc (không phóng to quá
  chi tiết thật); có thể sửa `ProcessOptions(min_zoom=..., max_zoom=...)` trong
  `processing.py` nếu cần ép cứng — UI hiện chưa có ô nhập zoom thủ công (làm sau nếu cần).

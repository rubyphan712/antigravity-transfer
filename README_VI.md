# Hướng Dẫn Chuyển Dữ Liệu Folder & Lịch Sử Chat Antigravity Từ Máy Này Sang Máy Khác

Tài liệu này hướng dẫn chi tiết **2 phương pháp** để chuyển dữ liệu dự án (Workspace Folder) và các đoạn lịch sử hội thoại (Chat History, Artifacts, Kế hoạch `/plan`, Cấu hình Project) của **Google Antigravity** sang máy tính khác:

1. **Cách 1 (Khuyên dùng)**: Sử dụng bộ công cụ tự động `run_transfer.bat` / `agy_transfer.py` đi kèm trong thư mục này — hỗ trợ **chuyển chọn lọc từng folder/đoạn chat (gộp an toàn không mất chat cũ ở máy đích)** hoặc **chuyển toàn bộ**, đồng thời **tự động sửa lại đường dẫn** nếu máy mới khác tên ổ đĩa (`D:\` ↔ `C:\`) hoặc khác tên User Windows (`C:\Users\Admin` ↔ `C:\Users\NewUser`).
2. **Cách 2**: Copy thủ công từng thư mục dành cho trường hợp chuyển toàn bộ sang máy mới có cùng cấu trúc đường dẫn.

---

## Phần 1: Cấu Trúc Lưu Trữ Dữ Liệu Của Antigravity Trên Máy

Khi bạn làm việc trên Antigravity, dữ liệu của một dự án và các cuộc trò chuyện không nằm ở một file duy nhất mà được liên kết qua **5 vị trí**:

| Thành phần | Đường dẫn trên Windows | Vai trò |
| :--- | :--- | :--- |
| **1. Thư mục dự án (Workspace)** | Ví dụ: `D:\TenProject\` | Chứa mã nguồn dự án và các tuỳ chỉnh cục bộ như `.agents\`, `GEMINI.md`, `AGENTS.md`. |
| **2. Định nghĩa Project** | `%USERPROFILE%\.gemini\config\projects\<project_id>.json` | Lưu tên Project và đường dẫn `folderUri` (ví dụ `file:///d%3A/TenProject`). |
| **3. Database từng cuộc hội thoại** | `%USERPROFILE%\.gemini\antigravity\conversations\<conv_id>.db` | File SQLite chứa toàn bộ các bước chat (`steps`) và khối nhị phân Protobuf (`trajectory_metadata_blob`) gắn liền với `folderUri` và `project_id`. |
| **4. Chỉ mục danh sách Chat & Tìm kiếm** | `%USERPROFILE%\.gemini\antigravity\conversation_summaries.db`<br>`%USERPROFILE%\.gemini\antigravity\agyhub_summaries_proto.pb`<br>`%USERPROFILE%\.gemini\antigravity\annotations\<conv_id>.pbtxt` | Hiển thị danh sách lịch sử chat ở thanh bên (Sidebar), gắn từng đoạn chat vào đúng Project và phục vụ tìm kiếm toàn văn (FTS5). |
| **5. Artifacts & Nhật ký (Brain)** | `%USERPROFILE%\.gemini\antigravity\brain\<conv_id>\` | Chứa các bản kế hoạch (`implementation_plan.md`), báo cáo (`walkthrough.md`), ảnh đính kèm và file `transcript.jsonl`. |

> **Tại sao copy thủ công dễ gặp lỗi khi khác đường dẫn hoặc gộp máy?**
> - Nếu máy đích **đã có sẵn các đoạn chat khác**, việc copy đè file `conversation_summaries.db` sẽ làm **mất toàn bộ danh sách chat cũ của máy đích**.
> - Nếu máy đích **đặt folder ở ổ đĩa khác** (ví dụ máy cũ `D:\MyApp`, máy mới `C:\Projects\MyApp`) hoặc **khác tên User Windows** (`C:\Users\Admin` sang `C:\Users\Minh`), đường dẫn lưu trong khối nhị phân Protobuf (`raw_summary` và `trajectory_metadata_blob`) sẽ bị lệch khiến đoạn chat không nhận diện đúng thư mục hoặc không mở được file Artifact.
> - Công cụ `agy_transfer.py` giải quyết triệt để cả 2 vấn đề trên bằng cách **UPSERT (gộp dòng)** vào SQLite và **tính toán lại độ dài `varint` Protobuf** cho đường dẫn mới.

---

## Phần 2: Cách 1 — Sử Dụng Công Cụ Tự Động (`run_transfer.bat` / `agy_transfer.py`)

### A. Giao diện Menu tương tác (Nhanh & Dễ nhất)

Chỉ cần nhấp đúp chuột vào file **`run_transfer.bat`** (hoặc mở Terminal chạy `python agy_transfer.py`):

#### Bước 1: Trên máy cũ (Máy nguồn)
1. Chạy `run_transfer.bat`.
2. Chọn **Phím `1`** để xem danh sách toàn bộ các Project, đường dẫn Folder và các đoạn chat đang có.
3. Chọn chế độ đóng gói:
   - **Phím `2` (Đóng gói CHỌN LỌC - Selective Export)**: Nhập số thứ tự Project muốn chuyển (ví dụ `1` hoặc `1,2`) hoặc nhập mã ID đoạn chat lẻ. Chọn `Y` khi được hỏi có đính kèm luôn mã nguồn trong Folder dự án vào file `.zip` hay không.
   - **Phím `3` (Đóng gói TOÀN BỘ - Full Backup)**: Xuất tất cả Projects, toàn bộ lịch sử chat, Brain/Artifacts và cấu hình Global (`config.json`, `mcp_config.json`).
4. Công cụ sẽ tạo ra 1 file `.zip` duy nhất (ngay cả khi Antigravity đang mở, nhờ cơ chế sao lưu nóng `sqlite3.backup()`).

#### Bước 2: Chuyển sang máy mới (Máy đích)
1. Copy thư mục công cụ `Tranfer-Antigravity` kèm file `.zip` vừa xuất sang máy mới (qua USB, Google Drive, LAN...).
2. **Đóng hoàn toàn ứng dụng Antigravity trên máy mới** (kiểm tra System Tray để chắc chắn `Antigravity.exe` đã tắt).
3. Chạy `run_transfer.bat` trên máy mới và chọn **Phím `5` (Import & Smart Remap)**:
   - Nhập đường dẫn tới file `.zip`.
   - Với từng Project trong gói, công cụ sẽ hỏi:
     - **Nhấn `Enter`** nếu muốn giữ nguyên đường dẫn giống hệt máy cũ (ví dụ cùng là `D:\TenProject`).
     - **Hoặc nhập đường dẫn mới trên máy đích** (ví dụ `C:\Code\TenProject`) — công cụ sẽ tự giải nén code ra thư mục mới và tự động cập nhật toàn bộ URI trong SQLite + Protobuf + đường dẫn User Windows!
4. Mở lại Antigravity trên máy mới: Project, thư mục code, toàn bộ lịch sử chat và các file Artifacts sẽ hiển thị đầy đủ bên cạnh các đoạn chat sẵn có của máy mới.

---

### B. Sử dụng qua dòng lệnh CLI (Dành cho Power Users / Tự động hoá)

```powershell
# 1. Liệt kê toàn bộ Projects & lịch sử chat trên máy
python agy_transfer.py list

# 2. Xuất chọn lọc 1 hoặc nhiều Project (theo tên, ID hoặc đường dẫn folder)
python agy_transfer.py export -o my_project_backup.zip --projects "Tranfer-Antigravity"

# 3. Xuất toàn bộ (Full Backup)
python agy_transfer.py export -o full_backup.zip --full

# 4. Kiểm tra nội dung bên trong file .zip trước khi Import
python agy_transfer.py inspect my_project_backup.zip

# 5. Import vào máy đích và giữ nguyên đường dẫn folder như máy cũ
python agy_transfer.py import my_project_backup.zip

# 6. Import vào máy đích VÀ đổi đường dẫn folder từ D:\OldFolder sang C:\Work\NewFolder
python agy_transfer.py import my_project_backup.zip --map "D:\Tranfer-Antigravity=C:\Work\Tranfer-Antigravity"

# 7. Hoàn tác (Rollback) về trạng thái trước khi Import nếu cần
python agy_transfer.py rollback "C:\Users\<TenUser>\.gemini\antigravity\backups\pre_import_YYYYMMDD_HHMMSS"
```

---

## Phần 3: Cách 2 — Chuyển Thủ Công Từng Bước (Manual Copy)

Nếu máy mới **chưa có lịch sử chat nào cần giữ lại** VÀ bạn đặt thư mục code ở **đường dẫn ổ đĩa giống hệt máy cũ** (ví dụ cùng là `D:\MyProject`), bạn có thể copy thủ công theo đúng 4 bước sau:

### Bước 1: Tắt hoàn toàn Antigravity ở cả 2 máy
- Đóng cửa sổ Antigravity và thoát hẳn ở khay hệ thống (System Tray).
- **Lý do**: Khi đang chạy, SQLite giữ dữ liệu tạm trong file `.db-wal` và `.db-shm`. Tắt ứng dụng giúp SQLite gộp hết dữ liệu vào file `.db` chính.

### Bước 2: Copy thư mục dự án (Workspace Folder)
- Copy thư mục dự án của bạn (ví dụ `D:\MyProject`) sang **đúng đường dẫn tương ứng** trên máy mới (`D:\MyProject`).

### Bước 3: Copy dữ liệu trong `%USERPROFILE%\.gemini`
Trên máy cũ, mở hộp thoại Run (`Win + R`), gõ `%USERPROFILE%\.gemini` và nhấn Enter. Copy các thư mục/file sau sang đúng `%USERPROFILE%\.gemini` trên máy mới:

1. **Trong thư mục `.gemini\config\`**:
   - Thư mục `projects\` (chứa các file `<project_id>.json`).
   - File `config.json` và `mcp_config.json` (nếu muốn chuyển cả cấu hình MCP Server và cài đặt chung).
2. **Trong thư mục `.gemini\antigravity\`**:
   - Thư mục `conversations\` (chứa toàn bộ các file `<conv_id>.db`).
   - Thư mục `brain\` (chứa toàn bộ Artifacts, kế hoạch `.md`, ảnh và logs).
   - Thư mục `annotations\` (chứa các file `.pbtxt` lưu tiêu đề hội thoại).
   - File `conversation_summaries.db` và `agyhub_summaries_proto.pb`.

### Bước 4: Mở Antigravity trên máy mới & Đăng nhập
- Lưu ý: **Không nên** copy đè thư mục `%APPDATA%\Antigravity\Local Storage` hoặc `Network` vì token đăng nhập được mã hoá theo phần cứng máy cũ (Windows DPAPI).
- Chỉ cần mở Antigravity trên máy mới và đăng nhập tài khoản Google của bạn như bình thường, toàn bộ lịch sử chat và Project trong `.gemini` sẽ tự động được nhận diện.

---

## Phần 4: Xử Lý Sự Cố Thường Gặp (Troubleshooting)

1. **Đã copy file `.db` vào `conversations\` nhưng mở Antigravity không thấy hiện trên thanh lịch sử bên trái?**
   - Nguyên nhân: Thanh bên (Sidebar) đọc danh sách từ `conversation_summaries.db` và `agyhub_summaries_proto.pb` chứ không tự quét thư mục `conversations\`.
   - Cách khắc phục: Sử dụng công cụ `run_transfer.bat` (chức năng Export & Import) để tự động đăng ký dòng chỉ mục vào `conversation_summaries.db` và `agyhub_summaries_proto.pb`.
2. **Báo lỗi `Antigravity.exe đang chạy` khi Import?**
   - Mở Task Manager (`Ctrl + Shift + Esc`), tìm `Antigravity` và chọn **End Task**, sau đó chạy lại lệnh Import.
3. **Muốn khôi phục lại trạng thái máy đích trước khi Import?**
   - Mỗi lần chạy Import, công cụ đều tự động lưu bản sao lưu tại `%USERPROFILE%\.gemini\antigravity\backups\pre_import_<ngay_gio>\`. Bạn chỉ cần mở `run_transfer.bat` -> chọn **Phím `6` (Hoàn tác / Rollback)** và chọn bản sao lưu tương ứng.

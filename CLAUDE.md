# SCV Controller — Project Memory

## Mục đích dự án

Web app Flask cho phép người dùng **chạy lại failed test cases** (dùng tool CLI `scv`) cho các **Android device** được cắm trên nhiều máy tính Windows trong cùng LAN — từ một máy host duy nhất, qua giao diện web.

### Bối cảnh nghiệp vụ
- Các script test (test cases) đã được chạy trên **Test Farm** với một **Test ID** cụ thể
- Các script bị **FAIL** sẽ được chạy lại trên máy tính local bằng tool `scv`
- `scv` là CLI tool, cần chạy trong **PowerShell 7**, với **working directory = thư mục run**
- `scv` đã có trong **PATH** của tất cả các máy đích — không cần đường dẫn tuyệt đối

---

## Kiến trúc

```
Host Machine (Flask Server)
│
├── Web UI (index.html) — người dùng cấu hình & xem log
├── Flask Backend (app.py) — REST API + Socket.IO
│   ├── /api/scan_devices  → SSH → adb devices → list SN
│   └── /api/run           → spawn threads → run scv
│
└── Paramiko SSH Client
        │  (port 22, OpenSSH Server)
        ▼
    Máy đích A, B, C... (Windows, cùng LAN)
        └── pwsh -EncodedCommand "..."
                └── scv <SN> <script_name>
```

### Threading model
- **1 thread per (máy, device)** — chạy song song giữa các device/máy khác nhau
- **Items trong cùng 1 device chạy tuần tự** (tránh xung đột ADB khi nhiều instance cùng target 1 phone)

---

## Cấu trúc thư mục project

```
C:\Users\haian\.gemini\antigravity-ide\scratch\scv-controller\
├── app.py                     # Flask backend chính
├── requirements.txt           # flask, flask-socketio, paramiko
├── setup_target_machine.ps1   # Script bật OpenSSH trên máy đích
└── templates\
    └── index.html             # Web UI (dark mode, vanilla JS)
```

---

## Cấu trúc thư mục kết quả (trên máy đích)

```
C:\scv_runs\                        ← base_working_dir (cấu hình được)
  └── 16092026\                     ← ngày chạy DDMMYYYY (tạo 1 lần, idempotent)
       ├── R52T30BXKGM_Camera_FARM001\   ← {SN}_{item}_{test_id}
       ├── R52T30BXKGM_Battery_FARM001\
       └── R52T30BXKGN_Camera_FARM002\
```

---

## Data Flow chi tiết

### 1. Scan Devices
```
POST /api/scan_devices
Body: { ip, username, password }

→ SSH connect (port 22)
→ exec: "adb devices"
→ parse stdout: lấy lines có status == "device"
→ trả về: { devices: ["SN1", "SN2", ...] }
```

### 2. Run
```
POST /api/run
Body: {
  base_working_dir: "C:\\scv_runs",
  machines: [
    {
      ip, username, password,
      devices: [
        {
          sn: "R52T30BXKGM",
          test_id: "FARM_2026_001",
          script_name: "camera_test.py",
          items: ["Camera", "Battery"]   ← labels, chỉ dùng đặt tên folder
        }
      ]
    }
  ]
}

→ Tạo job_id (uuid 8 chars)
→ Spawn thread per (machine, device)
→ Trả về: { job_id, sessions: [{session_id, ip, sn, item}] }
```

### 3. Thread per device (run_device)
```
1. SSH connect → emit status: "connecting"
2. mkdir DDMMYYYY\           (New-Item -Force, idempotent)
3. For each item (sequential):
   a. emit status: "running"
   b. mkdir {SN}_{item}_{test_id}\  (New-Item -Force)
   c. pwsh -EncodedCommand "Set-Location '<folder>'; scv <SN> <script_name> 2>&1"
   d. stream stdout → socketio.emit("log", ...)
   e. check exit code → emit status: "success" | "failed"
4. SSH close
```

---

## scv Command Format

> ⚠️ **TODO**: Format lệnh chưa được xác nhận chính xác. Cần update khi có thông tin.

**Vị trí trong code**: `app.py`, function `_run_device()`, dòng:
```python
# TODO: Update command format when exact syntax is confirmed.
# Current format: scv <SN> <script_name>
scv_cmd = f"scv {sn} {script_name} 2>&1"
```

**Thông tin đã biết:**
- `scv` có tham số SN (serial number của device)
- `scv` có tham số tên script hoặc test suite
- `scv` trong PATH → gọi trực tiếp, không cần đường dẫn

---

## PowerShell — Kỹ thuật quan trọng

### Base64 EncodedCommand
Tất cả lệnh PS được encode thành Base64 UTF-16LE để tránh quoting issues:
```python
def encode_ps(script: str) -> str:
    return base64.b64encode(script.encode('utf-16-le')).decode('ascii')

# Usage:
cmd = f"pwsh -NonInteractive -EncodedCommand {encode_ps(script)}"
```

### Stream output realtime
```python
# Dùng channel riêng (không phải exec_command) để stream
transport = client.get_transport()
channel = transport.open_session()
channel.set_combine_stderr(True)
channel.exec_command(cmd)
# loop recv(4096) cho đến exit_status_ready()
```

---

## Socket.IO Events

| Event | Direction | Payload |
|---|---|---|
| `log` | Server → Client | `{session_id, ip, sn, item, level, message, timestamp}` |
| `status_update` | Server → Client | `{session_id, ip, sn, item, job_id, status}` |

**Status values**: `queued` → `connecting` → `running` → `success` / `failed`

**session_id format**: `{job_id}_{ip_clean}_{sn}_{item}`  
(ip_clean = ip với dấu `.` thay bằng `_`)

---

## Setup máy đích (1 lần)

Chạy `setup_target_machine.ps1` với quyền **Administrator**:
- Cài OpenSSH Server (Windows built-in feature)
- Start service sshd + auto-start
- Mở Firewall port 22
- Đặt PowerShell 7 làm default shell cho SSH

**Yêu cầu trên máy đích:**
- Windows 10/11
- PowerShell 7 (`pwsh`) đã cài
- ADB trong PATH
- `scv` trong PATH
- OpenSSH Server enabled

---

## Chạy server (máy host)

```powershell
cd C:\Users\haian\.gemini\antigravity-ide\scratch\scv-controller
python app.py
# → http://0.0.0.0:5000
```

**Dependencies:**
```
flask>=3.0.0
flask-socketio>=5.3.0
paramiko>=3.0.0
```

---

## UI Components

### Left Panel
- **Cấu hình chung**: base_working_dir + items list (tag chips)
- **Máy đích**: Machine cards với IP/user/pass + Scan button
- **Device cards** (sau scan): checkbox + item chips + Test ID + Script/Suite
- **Chạy tất cả button**

### Right Panel (Terminals)
- Grouped: `🖥️ Machine IP` → `📱 Device SN` → `🧪 Item terminal`
- Mỗi terminal: header (item name + status badge) + scrollable log body (195px)
- Status badge: Queued / Connecting / Running / Success / Failed

---

## Decisions & Rationale

| Quyết định | Lý do |
|---|---|
| SSH (OpenSSH) thay WinRM | Bảo mật hơn, Windows 10/11 có built-in, Paramiko ổn định |
| Base64 EncodedCommand | Tránh quoting hell khi path có spaces hay ký tự đặc biệt |
| Items chạy tuần tự per device | Tránh conflict ADB khi 2 instance cùng target 1 phone |
| Threading (không asyncio/eventlet) | Đơn giản, ổn định, eventlet deprecated |
| Items là labels thuần | Chỉ dùng đặt tên folder, không ảnh hưởng lệnh scv |
| Script/Suite per device | Mỗi device có thể cần chạy script khác nhau |

---

## Known TODOs

- [ ] **Xác nhận format lệnh `scv`** và update `_run_device()` trong `app.py`
- [ ] Thêm tính năng lưu log ra file trên máy đích
- [ ] Thêm xác thực SSH Key thay username/password
- [ ] Thêm timeout configurable cho từng run
- [ ] Thêm tính năng cancel job đang chạy

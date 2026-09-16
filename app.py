import base64
import datetime
import threading
import time
import uuid

import paramiko
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

app = Flask(__name__)
app.config['SECRET_KEY'] = 'scv-controller-secret-key'
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins='*')

# In-memory job store
jobs: dict = {}

BASE_WORKING_DIR = r"C:\scv_runs"


# ──────────────────────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────────────────────

def encode_ps(script: str) -> str:
    """Encode a PowerShell script to UTF-16LE Base64 for -EncodedCommand."""
    return base64.b64encode(script.encode('utf-16-le')).decode('ascii')


def ssh_connect(ip: str, username: str, password: str, timeout: int = 15) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(ip, port=22, username=username, password=password, timeout=timeout)
    return client


def exec_ps(client: paramiko.SSHClient, script: str, timeout: int = 30):
    """Run a PS script via SSH and return (stdout, stderr) strings."""
    cmd = f"pwsh -NonInteractive -EncodedCommand {encode_ps(script)}"
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    return stdout.read().decode(errors='replace').strip(), stderr.read().decode(errors='replace').strip()


def stream_ps(client: paramiko.SSHClient, script: str, on_line) -> int:
    """Run PS script, stream stdout line-by-line to on_line(msg, level). Returns exit code."""
    cmd = f"pwsh -NonInteractive -EncodedCommand {encode_ps(script)}"
    transport = client.get_transport()
    channel = transport.open_session()
    channel.set_combine_stderr(True)
    channel.exec_command(cmd)

    buf = ""
    while True:
        if channel.recv_ready():
            chunk = channel.recv(4096).decode(errors='replace')
            buf += chunk
            while '\n' in buf:
                line, buf = buf.split('\n', 1)
                stripped = line.rstrip()
                if stripped:
                    on_line(stripped, "OUTPUT")
        elif channel.exit_status_ready():
            rest = channel.recv(65535).decode(errors='replace')
            buf += rest
            for line in buf.splitlines():
                if line.strip():
                    on_line(line.rstrip(), "OUTPUT")
            break
        else:
            time.sleep(0.05)

    return channel.recv_exit_status()


# ──────────────────────────────────────────────────────────────
#  ROUTES
# ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/scan_devices', methods=['POST'])
def scan_devices():
    """SSH into a machine and return list of ADB-connected device serial numbers."""
    data = request.get_json()
    ip       = data.get('ip', '').strip()
    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not ip or not username:
        return jsonify({'error': 'IP và username là bắt buộc'}), 400

    try:
        client = ssh_connect(ip, username, password, timeout=10)
        _, stdout, _ = client.exec_command('adb devices', timeout=15)
        output = stdout.read().decode(errors='replace')
        client.close()

        devices = []
        for line in output.splitlines()[1:]:   # skip "List of devices attached"
            line = line.strip()
            if '\t' in line:
                sn, status = line.split('\t', 1)
                if status.strip() == 'device':
                    devices.append(sn.strip())

        return jsonify({'devices': devices, 'ip': ip})

    except paramiko.AuthenticationException:
        return jsonify({'error': f'Sai username/password cho {ip}'}), 401
    except Exception as e:
        return jsonify({'error': f'{type(e).__name__}: {str(e)}'}), 500


@app.route('/api/run', methods=['POST'])
def api_run():
    """Start scv runs for all selected (machine, device, item) combinations."""
    data          = request.get_json()
    machines_data = data.get('machines', [])
    base_dir      = data.get('base_working_dir', BASE_WORKING_DIR)

    if not machines_data:
        return jsonify({'error': 'Chưa có máy nào được cấu hình'}), 400

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {'id': job_id, 'created_at': datetime.datetime.now().isoformat(), 'runs': {}}

    sessions = []

    for machine in machines_data:
        ip       = machine.get('ip', '').strip()
        username = machine.get('username', '').strip()
        password = machine.get('password', '')
        devices  = machine.get('devices', [])

        if not ip or not username:
            continue

        for device in devices:
            sn          = device.get('sn', '').strip()
            test_id     = device.get('test_id', '').strip()
            script_name = device.get('script_name', '').strip()
            items       = [i.strip() for i in device.get('items', []) if i.strip()]

            if not sn or not items:
                continue

            ip_clean = ip.replace('.', '_')

            for item in items:
                run_key    = f"{ip}|{sn}|{item}"
                session_id = f"{job_id}_{ip_clean}_{sn}_{item}"
                jobs[job_id]['runs'][run_key] = {
                    'session_id': session_id,
                    'ip': ip, 'sn': sn, 'item': item,
                    'test_id': test_id, 'script_name': script_name,
                    'status': 'queued', 'logs': []
                }
                sessions.append({'session_id': session_id, 'ip': ip, 'sn': sn, 'item': item})

            # One thread per device — items run sequentially within the thread
            t = threading.Thread(
                target=_run_device,
                args=(job_id, ip, username, password, sn, test_id, script_name, items, base_dir),
                daemon=True
            )
            t.start()

    return jsonify({'job_id': job_id, 'sessions': sessions, 'session_count': len(sessions)})


@app.route('/api/jobs', methods=['GET'])
def api_jobs():
    result = {}
    for jid, job in jobs.items():
        result[jid] = {
            'id': jid,
            'created_at': job['created_at'],
            'runs': {k: {'status': v['status']} for k, v in job['runs'].items()}
        }
    return jsonify(result)


# ──────────────────────────────────────────────────────────────
#  DEVICE RUNNER  (one SSH session, items sequential)
# ──────────────────────────────────────────────────────────────

def _run_device(job_id, ip, username, password, sn, test_id, script_name, items, base_dir):
    ip_clean = ip.replace('.', '_')

    def session_id(item):
        return f"{job_id}_{ip_clean}_{sn}_{item}"

    def emit_log(item, msg, level="INFO"):
        sid   = session_id(item)
        entry = {
            'session_id': sid, 'ip': ip, 'sn': sn, 'item': item,
            'level': level, 'message': msg,
            'timestamp': datetime.datetime.now().strftime('%H:%M:%S')
        }
        run_key = f"{ip}|{sn}|{item}"
        if job_id in jobs and run_key in jobs[job_id]['runs']:
            jobs[job_id]['runs'][run_key]['logs'].append(entry)
        socketio.emit('log', entry)

    def emit_status(item, status):
        sid     = session_id(item)
        run_key = f"{ip}|{sn}|{item}"
        if job_id in jobs and run_key in jobs[job_id]['runs']:
            jobs[job_id]['runs'][run_key]['status'] = status
        socketio.emit('status_update', {
            'session_id': sid, 'ip': ip, 'sn': sn, 'item': item,
            'job_id': job_id, 'status': status
        })

    # Mark all items as "connecting" while opening SSH
    for item in items:
        emit_status(item, 'connecting')

    try:
        client = ssh_connect(ip, username, password)
        emit_log(items[0], f'✓ Đã kết nối SSH tới {ip}', 'SUCCESS')

        today    = datetime.datetime.now().strftime('%d%m%Y')
        date_dir = f"{base_dir}\\{today}"

        # ── Step 1: Create date folder (idempotent) ────────────────
        out, err = exec_ps(client,
            f"New-Item -ItemType Directory -Force -Path '{date_dir}' | Out-Null; Write-Output 'OK'"
        )
        if 'OK' not in out:
            for item in items:
                emit_log(item, f'Không thể tạo thư mục ngày: {date_dir}\n{err}', 'ERROR')
                emit_status(item, 'failed')
            client.close()
            return

        emit_log(items[0], f'📁 Thư mục ngày: {date_dir}', 'INFO')

        # ── Step 2: Run each item sequentially ────────────────────
        for item in items:
            emit_status(item, 'running')
            run_folder = f"{date_dir}\\{sn}_{item}_{test_id}"

            emit_log(item, f'┌── Item: {item} ─────────────────')
            emit_log(item, f'│  SN      : {sn}')
            emit_log(item, f'│  Item    : {item}')
            emit_log(item, f'│  Test ID : {test_id}')
            emit_log(item, f'│  Script  : {script_name or "(chưa cấu hình)"}')
            emit_log(item, f'│  Folder  : {run_folder}')

            # Create item run folder
            out, err = exec_ps(client,
                f"New-Item -ItemType Directory -Force -Path '{run_folder}' | Out-Null; Write-Output 'OK'"
            )
            if 'OK' not in out:
                emit_log(item, f'│  ✗ Lỗi tạo folder: {err}', 'ERROR')
                emit_log(item, '└──────────────────────────────────')
                emit_status(item, 'failed')
                continue

            emit_log(item, '│  ✓ Folder tạo thành công', 'SUCCESS')
            emit_log(item, '│  ▶ Đang chạy scv...')
            emit_log(item, '└──────────────────────────────────')

            # ── Build scv command ──────────────────────────────────
            # TODO: Update command format when exact syntax is confirmed.
            # Current format: scv <SN> <script_name>
            # Modify the line below to match the actual scv CLI.
            if script_name:
                scv_cmd = f"scv {sn} {script_name} 2>&1"
            else:
                scv_cmd = f"scv {sn} 2>&1"

            ps_script = f"Set-Location '{run_folder}'; {scv_cmd}"

            exit_code = stream_ps(
                client, ps_script,
                lambda msg, lvl, _item=item: emit_log(_item, msg, lvl)
            )

            emit_log(item, '─' * 40)
            if exit_code == 0:
                emit_log(item, f'✓ scv hoàn thành (exit: {exit_code})', 'SUCCESS')
                emit_status(item, 'success')
            else:
                emit_log(item, f'✗ scv thất bại (exit: {exit_code})', 'ERROR')
                emit_status(item, 'failed')

        client.close()

    except paramiko.AuthenticationException:
        for item in items:
            emit_log(item, f'Xác thực thất bại cho {ip}. Kiểm tra username/password.', 'ERROR')
            emit_status(item, 'failed')
    except paramiko.ssh_exception.NoValidConnectionsError:
        for item in items:
            emit_log(item, f'Không thể kết nối {ip}:22. SSH đang chạy không?', 'ERROR')
            emit_status(item, 'failed')
    except Exception as e:
        for item in items:
            emit_log(item, f'Lỗi: {type(e).__name__}: {str(e)}', 'ERROR')
            emit_status(item, 'failed')


# ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print("=" * 60)
    print("  SCV Controller  —  http://0.0.0.0:5000")
    print("=" * 60)
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, allow_unsafe_werkzeug=True)

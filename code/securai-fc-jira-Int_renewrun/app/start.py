import subprocess
import sys
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
import json

# ===================== 核心配置 =====================
SERVER_PORT = 1000
SERVER_HOST = "0.0.0.0"


def run_script(script_name):
    """执行单个 Python 脚本"""
    print(f"\n{'=' * 20} 正在开始执行: {script_name} {'=' * 20}")
    try:
        subprocess.run(
            [sys.executable, script_name],
            check=True,
            text=True
        )
        print(f"✅ 执行成功: {script_name}")
        return True
    except Exception as e:
        print(f"❌ 执行失败: {script_name}, 错误: {str(e)}")
        return False


def task_logic():

    current_dir = os.path.dirname(os.path.abspath(__file__))


    scripts_to_run = [
        os.path.join(current_dir, "jira_get_need_renewal.py"),
        os.path.join(current_dir, "jira_renewal_resources.py"),
        os.path.join(current_dir, "jira_upstat_renewaldone_sbu_issues.py")
    ]

    for script in scripts_to_run:
        if not os.path.exists(script):
            return {"code": 500, "msg": f"找不到脚本: {script}"}
        if not run_script(script):
            return {"code": 500, "msg": f"脚本执行中断: {script}"}

    return {"code": 200, "msg": "所有任务执行成功"}


# ===================== HTTP Server 逻辑 =====================
class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """处理健康检查"""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok"}).encode())

    def do_POST(self):
        """FC 触发器调用时执行"""
        print("📥 收到触发请求，开始执行任务...")
        result = task_logic()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


def start_server():
    server = HTTPServer((SERVER_HOST, SERVER_PORT), RequestHandler)
    print(f"📡 Server started on port {SERVER_PORT}...")
    server.serve_forever()


if __name__ == "__main__":
    start_server()
import subprocess
import sys
import os
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
import json

# ===================== 核心配置 =====================
SERVER_PORT = 1000
SERVER_HOST = "0.0.0.0"


def run_script(script_name):
    """执行单个 Python 脚本"""
    print(f"\n{'=' * 20} 正在开始执行: {script_name} {'=' * 20}")
    try:
        # 使用 sys.executable 确保环境一致
        result = subprocess.run(
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
    """原有的 main 逻辑，封装为任务函数"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    now = datetime.now()
    day = now.day
    weekday = now.weekday()

    scripts_to_run = []

    # A: 每月1号 13 点前
    if day == 1 and now.hour < 5:
        scripts_to_run = [
            os.path.join(current_dir, "data_clean.py"),
            os.path.join(current_dir, "jira_get_resale_business_details.py"),
            os.path.join(current_dir, "jira_get_account.py"),
            os.path.join(current_dir, "jira_get_request_type.py"),
            os.path.join(current_dir, "aliyun_get_bill.py"),
            os.path.join(current_dir, "add_tag.py"),
            os.path.join(current_dir, "aliyun_get_income.py"),
            os.path.join(current_dir, "jira_reset_total_expenditure.py"),
            os.path.join(current_dir, "jira_reset_total_income.py"),
            os.path.join(current_dir, "jira_create_bill_issue.py"),
            #续费
            os.path.join(current_dir, "aliyun_get_renewal_endtime.py"), 
            os.path.join(current_dir, "jira_get_renewal_issues.py"), 
            os.path.join(current_dir, "jira_get_renewal_sub_issues.py"), 
            os.path.join(current_dir, "jira_create_renewal_issue.py"),
            os.path.join(current_dir, "jira_delete_renewal_issue.py"),
            os.path.join(current_dir, "jira_upstat_sbu_issues.py"),  
            os.path.join(current_dir, "jira_get_renewal_issues.py"),
            os.path.join(current_dir, "jira_get_renewal_sub_issues.py"),
            os.path.join(current_dir, "jira_reset_renewal_endtime.py"),
            os.path.join(current_dir, "jira_get_renewal_sub_issues.py"),
            os.path.join(current_dir, "jira_upstat_renewal_sbu_issues.py") 
        ]

    # A: 每月1号 13 点后
    elif day == 1 and now.hour > 5:
        scripts_to_run = [
            os.path.join(current_dir, "jira_get_need_renewal.py"),
            os.path.join(current_dir, "jira_renewal_resources.py"),
            os.path.join(current_dir, "jira_upstat_renewaldone_sbu_issues.py")
        ]


    # B: 每月4号
    elif day == 4:
        scripts_to_run = [
            os.path.join(current_dir, "record_rate.py"),
            os.path.join(current_dir, "jira_get_request_type.py"),
            os.path.join(current_dir, "jira_get_resale_business_details.py"),
            os.path.join(current_dir, "jira_get_account.py"),
            os.path.join(current_dir, "make_monthly_report.py"),
            os.path.join(current_dir, "jira_create_report_issue.py")
        ]
    # B: 测试
    elif day == 7:
        scripts_to_run = [
            os.path.join(current_dir, "jira_create_report_issue.py")
        ]

    # C: 每周一及每月2号确认余额
    elif weekday == 0 or day == 2:
        scripts_to_run = [
            os.path.join(current_dir, "aliyun_query_balance.py"),
            os.path.join(current_dir, "jira_get_resale_business_details.py"),
            os.path.join(current_dir, "jira_get_account.py"),
            os.path.join(current_dir, "jira_get_request_type.py"),
            os.path.join(current_dir, "jira_reset_balance.py")
        ]
     
    else:
        return {"code": 200, "msg": f"今日 ({now.strftime('%Y-%m-%d')}) 无任务"}

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
        """FC 触发器调用时通常发送 POST /invoke"""
        print("📥 收到触发请求，开始执行任务...")
        result = task_logic()

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())


def start_server():
    server = HTTPServer((SERVER_HOST, SERVER_PORT), RequestHandler)
    print(f"📡 Server started on port {SERVER_PORT}...")
    # 这里是关键：让 HTTP 服务在主线程运行，阻塞住不让进程退出
    server.serve_forever()


if __name__ == "__main__":
    # 阿里云 FC Custom Runtime 启动后，必须保持端口监听
    # 不要在这里直接跑 task_logic()，除非你想让函数只运行一次就结束（不推荐）
    start_server()
import requests
from requests.auth import HTTPBasicAuth
import configparser
import os
import logging
from datetime import datetime, timedelta
import pandas as pd


# ===================== 全局配置与工具函数 =====================
def clean_str(value):
    """简单的字符串清洗"""
    if pd.isna(value) or value is None:
        return ""
    return str(value).strip()


def init_logger():
    """初始化日志配置"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.normpath(os.path.join(current_dir, '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'jira_cleanup_task.log')

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    logger = logging.getLogger(__name__)
    logger.info("=" * 80)
    logger.info(f"程序启动：Jira过期任务清理（时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")
    logger.info("=" * 80)
    return logger


# ===================== 配置管理类 =====================
class CleanupConfig:
    def __init__(self, logger):
        self.logger = logger
        self.current_dir = os.path.dirname(os.path.abspath(__file__))
        self.target_month = self._get_target_month()  # 用于自动匹配账单文件名

    def _get_target_month(self):
        """获取上个月的日期（格式：YYYY-MM）"""
        today = datetime.now()
        first_day_of_current = today.replace(day=1)
        last_day_of_last = first_day_of_current - timedelta(days=1)
        return last_day_of_last.strftime("%Y-%m")

    def load_jira_config(self):
        """加载Jira配置文件"""
        config_path = os.path.normpath(os.path.join(self.current_dir, '../../jira/config/jira_config.ini'))
        if not os.path.exists(config_path):
            self.logger.error(f"Jira配置文件不存在：{config_path}")
            raise FileNotFoundError(f"Jira配置文件不存在：{config_path}")

        config = configparser.ConfigParser()
        config.read(config_path, encoding='utf-8')
        return {
            'domain': config.get('jira', 'domain'),
            'username': config.get('jira', 'user_name'),
            'token': config.get('jira', 'access_token')
        }

    def get_file_paths(self):
        """生成所有需要的数据文件路径"""
        data_dir = os.path.normpath(os.path.join(self.current_dir, '../../jira/data'))
        paths = {
            # 白名单文件（保留的依据）
            'account_mapping': os.path.join(data_dir, "jira_get_account.csv"),
            'renewal_bill': os.path.join(data_dir, f"aliyun_renewal_bill_{self.target_month}.csv"),

            # 待检查文件（需要清理的对象）
            'parent_issues': os.path.join(data_dir, "jira_get_renewal_issues.csv"),
            'sub_issues': os.path.join(data_dir, "jira_get_renewal_sbu_issues.csv")
        }
        for k, v in paths.items():
            self.logger.info(f"{k} 路径: {v}")
        return paths


# ===================== Jira 操作类 (修改版) =====================
class JiraCleaner:
    def __init__(self, jira_config, logger):
        self.domain = jira_config['domain']
        self.auth = HTTPBasicAuth(jira_config['username'], jira_config['token'])
        self.logger = logger
        self.headers = {"Accept": "application/json"}

    def delete_issue(self, issue_key):
        """
        删除 Jira Issue (修改版：增加级联删除参数)
        """
        # 关键修改：添加 deleteSubtasks=true 参数
        # 如果该 Issue 是父任务，这会强制删除其下所有子任务
        url = f"https://{self.domain}/rest/api/3/issue/{issue_key}?deleteSubtasks=true"

        try:
            self.logger.info(f"   正在尝试删除: {issue_key} (含级联删除参数)...")
            response = requests.delete(url, auth=self.auth, headers=self.headers, timeout=30)

            if response.status_code == 204:
                self.logger.info(f"   ✅ 删除成功: {issue_key}")
                return True
            else:
                self.logger.error(f"   ❌ 删除失败: {issue_key} (状态码: {response.status_code})")
                # 尝试解析更详细的错误信息
                try:
                    error_detail = response.json()
                    self.logger.error(f"   错误详情: {error_detail}")
                except:
                    self.logger.error(f"   响应内容: {response.text}")
                return False
        except Exception as e:
            self.logger.error(f"   ❌ 删除异常: {issue_key}, 错误: {str(e)}")
            return False


# ===================== 业务逻辑类 =====================
class CleanupManager:
    def __init__(self, config, jira_cleaner, logger):
        self.config = config
        self.jira_cleaner = jira_cleaner
        self.logger = logger
        self.paths = config.get_file_paths()

    def _read_csv_safe(self, file_path, required_cols):
        """安全读取CSV并检查列"""
        if not os.path.exists(file_path):
            self.logger.error(f"文件缺失: {file_path}")
            raise FileNotFoundError(f"文件缺失: {file_path}")

        df = pd.read_csv(file_path)
        for col in required_cols:
            if col not in df.columns:
                self.logger.error(f"文件 {file_path} 缺少必要列: {col}")
                raise ValueError(f"文件缺少必要列: {col}")
        return df

    def process_parent_issues(self):
        """处理父任务清理逻辑"""
        self.logger.info("\n" + "=" * 30 + " 开始检查父任务 " + "=" * 30)

        # 1. 读取白名单（账号映射）
        account_df = self._read_csv_safe(self.paths['account_mapping'], ['资源所属账号'])
        valid_accounts = set(clean_str(acc) for acc in account_df['资源所属账号'] if clean_str(acc))
        self.logger.info(f"白名单有效账号数量: {len(valid_accounts)}")

        # 2. 读取现有的父任务列表
        # 假设 jira_get_renewal_issues.csv 包含 '资源所属账号' 和 'Issue Key'
        try:
            parent_df = self._read_csv_safe(self.paths['parent_issues'], ['资源所属账号', 'Issue Key'])
        except Exception as e:
            self.logger.warning(f"跳过父任务处理: {e}")
            return

        # 3. 筛选需要删除的
        to_delete = []
        for _, row in parent_df.iterrows():
            account = clean_str(row['资源所属账号'])
            issue_key = clean_str(row['Issue Key'])

            if not issue_key: continue

            if account not in valid_accounts:
                self.logger.info(f"发现待删父任务: {issue_key} (账号: {account} 不在白名单)")
                to_delete.append(issue_key)

        # 4. 执行删除
        if not to_delete:
            self.logger.info("没有需要删除的父任务。")
            return

        self.logger.warning(f"准备删除 {len(to_delete)} 个父任务...")
        count = 0
        for key in to_delete:
            if self.jira_cleaner.delete_issue(key):
                count += 1
        self.logger.info(f"父任务清理完成: 成功删除 {count}/{len(to_delete)}")

    def process_sub_issues(self):
        """处理子任务清理逻辑"""
        self.logger.info("\n" + "=" * 30 + " 开始检查子任务 " + "=" * 30)

        # 1. 读取白名单（最新续费账单）
        try:
            bill_df = self._read_csv_safe(self.paths['renewal_bill'], ['资源id'])
            valid_resource_ids = set(clean_str(r_id) for r_id in bill_df['资源id'] if clean_str(r_id))
            self.logger.info(f"账单有效资源ID数量: {len(valid_resource_ids)}")
        except Exception as e:
            self.logger.warning(f"跳过子任务处理: {e}")
            return

        # 2. 读取现有的子任务列表
        # 假设 jira_get_renewal_sbu_issues.csv 包含 'ID' (资源ID) 和 'Issue Key'
        try:
            sub_df = self._read_csv_safe(self.paths['sub_issues'], ['ID', 'Issue Key'])
        except Exception as e:
            self.logger.warning(f"跳过子任务处理: {e}")
            return

        # 3. 筛选需要删除的
        to_delete = []
        for _, row in sub_df.iterrows():
            res_id = clean_str(row['ID'])
            issue_key = clean_str(row['Issue Key'])

            if not issue_key: continue

            if res_id not in valid_resource_ids:
                self.logger.info(f"发现待删子任务: {issue_key} (资源ID: {res_id} 不在账单中)")
                to_delete.append(issue_key)

        # 4. 执行删除
        if not to_delete:
            self.logger.info("没有需要删除的子任务。")
            return

        self.logger.warning(f"准备删除 {len(to_delete)} 个子任务...")
        count = 0
        for key in to_delete:
            if self.jira_cleaner.delete_issue(key):
                count += 1
        self.logger.info(f"子任务清理完成: 成功删除 {count}/{len(to_delete)}")

    def run(self):
        self.process_parent_issues()
        self.process_sub_issues()


# ===================== 主入口 =====================
def main():
    logger = init_logger()
    try:
        config = CleanupConfig(logger)
        jira_config = config.load_jira_config()
        cleaner = JiraCleaner(jira_config, logger)
        manager = CleanupManager(config, cleaner, logger)

        manager.run()

    except Exception as e:
        logger.error(f"程序执行异常：{str(e)}", exc_info=True)
    finally:
        logger.info("=" * 80)
        logger.info(f"程序结束（时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")
        logger.info("=" * 80)


if __name__ == "__main__":
    main()
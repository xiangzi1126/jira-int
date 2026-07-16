import requests
from requests.auth import HTTPBasicAuth
import configparser
import os
import logging
from datetime import datetime
import pandas as pd
import re
import json
# 导入附件上传函数
from jira_upload_attachment import upload_jira_attachment


# ===================== 全局配置 & 工具函数 =====================
def clean_json_value(value, field_name=None):
    """
    清洗JSON序列化值，适配Jira字段格式
    :param value: 原始值
    :param field_name: 字段名（用于金额字段特殊处理）
    :return: 清洗后的值
    """
    # 处理pandas特殊类型
    if isinstance(value, (pd.Int64Dtype, pd.Float64Dtype)):
        value = value if not pd.isna(value) else ""

    # 金额字段特殊处理（保留5位小数）
    amount_fields = ['customfield_10491', '消费金额']
    if field_name in amount_fields:
        if pd.isna(value) or value == "" or value is None:
            return "0.00"
        try:
            return f"{float(value):.5f}"
        except (ValueError, TypeError):
            return "0.00"

    # 空值处理
    if pd.isna(value) or value is None:
        return ""
    # 基础类型直接返回
    elif isinstance(value, (float, int, bool)):
        return value
    # 其他类型转为字符串
    else:
        return str(value)


def init_logger():
    """初始化日志配置（独立日志文件+控制台输出）"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.normpath(os.path.join(current_dir, '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'create_issue_with_attachment.log')

    # 配置日志格式（时间戳+级别+模块+信息）
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(funcName)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),  # 写入文件（追加模式）
            logging.StreamHandler()  # 输出到控制台
        ]
    )
    return logging.getLogger(__name__)


# 初始化日志实例
logger = init_logger()


# ===================== 核心业务逻辑 =====================
class JiraBillSync:
    def __init__(self):
        """初始化配置和路径"""
        self.logger = logger
        self.current_dir = os.path.dirname(os.path.abspath(__file__))
        self.config = self._load_config()
        self.jira_domain = self.config.get('jira', 'domain')
        self.user_name = self.config.get('jira', 'user_name')
        self.access_token = self.config.get('jira', 'access_token')

        # 路径配置
        self.data_dir = os.path.normpath(os.path.join(self.current_dir, '../../jira/data'))
        self.target_month = self._get_last_month()
        self.bill_file_path = self._get_bill_file_path()

    def _load_config(self):
        """加载Jira配置文件"""
        config_path = os.path.normpath(os.path.join(self.current_dir, '../../jira/config/jira_config.ini'))
        if not os.path.exists(config_path):
            self.logger.error(f"配置文件不存在: {config_path}")
            raise FileNotFoundError(f"配置文件缺失: {config_path}")

        config = configparser.ConfigParser()
        config.read(config_path, encoding='utf-8')
        self.logger.info(f"成功加载配置文件: {config_path}")
        return config

    def _get_last_month(self):
        """获取上个月（格式：YYYY-MM）"""
        today = datetime.now()
        if today.month == 1:
            last_month = f"{today.year - 1}-12"
        else:
            last_month = f"{today.year}-{today.month - 1:02d}"
        self.logger.info(f"目标账单月份: {last_month}")
        return last_month

    def _get_bill_file_path(self):
        """获取目标账单文件路径"""
        target_bill_file = f"aliyun_bill_{self.target_month}.csv"
        bill_file_path = os.path.join(self.data_dir, target_bill_file)

        if not os.path.exists(bill_file_path):
            self.logger.error(f"账单文件不存在: {bill_file_path}")
            raise FileNotFoundError(f"账单文件缺失: {bill_file_path}")

        self.logger.info(f"成功定位账单文件: {bill_file_path}")
        return bill_file_path

    def _load_mapping_files(self):
        """加载映射文件（账号映射+请求类型映射）"""
        # 账号映射文件
        account_map_path = os.path.join(self.data_dir, 'jira_get_account.csv')
        if not os.path.exists(account_map_path):
            self.logger.error(f"账号映射文件缺失: {account_map_path}")
            raise FileNotFoundError(f"账号映射文件缺失")

        # 请求类型映射文件
        rt_map_path = os.path.join(self.data_dir, 'jira_request_type.csv')
        if not os.path.exists(rt_map_path):
            self.logger.error(f"请求类型映射文件缺失: {rt_map_path}")
            raise FileNotFoundError(f"请求类型映射文件缺失")

        # 读取并预处理
        account_df = pd.read_csv(account_map_path)
        account_df['标签'] = account_df['标签'].fillna("").astype(str)

        request_type_df = pd.read_csv(rt_map_path)

        self.logger.info(f"加载映射文件完成 - 账号映射: {len(account_df)}条, 请求类型: {len(request_type_df)}条")
        return account_df, request_type_df

    def _load_bill_data(self):
        """加载并预处理账单数据"""
        bill_df = pd.read_csv(self.bill_file_path)
        # 补充标签列（防止缺失）
        if '标签' not in bill_df.columns:
            bill_df['标签'] = ""
        bill_df['标签'] = bill_df['标签'].fillna("").astype(str)

        self.logger.info(f"加载账单数据完成 - 总记录数: {len(bill_df)}, 账号数: {bill_df['资源所属账号'].nunique()}")
        return bill_df

    def _get_request_type_id(self, project_key, target_request_type="课金代行账单"):
        """获取指定项目的请求类型ID"""
        _, request_type_df = self._load_mapping_files()
        match_rt = request_type_df[
            (request_type_df['项目键'] == project_key) &
            (request_type_df['Request Type名称'] == target_request_type)
            ]

        if match_rt.empty:
            self.logger.warning(f"项目{project_key}未找到请求类型: {target_request_type}")
            return None

        rt_id = match_rt['Request TypeID'].values[0]
        self.logger.debug(f"项目{project_key}请求类型ID: {rt_id}")
        return rt_id

    def _create_jira_issue(self, bill_account, map_tag, project_key, target_bill_df):
        """创建Jira Issue并上传附件"""
        # 1. 构建Issue摘要和描述
        summary = f"【账单】{self.target_month} - {bill_account}"
        summary_text = (
            f"账单周期: {self.target_month}\n"
            f"账号: {bill_account}\n"
            f"标签: {map_tag if map_tag else '全部'}\n"
            f"明细条数: {len(target_bill_df)} 条\n\n"
            f"请查看附件中的 CSV 文件获取完整明细。"
        )

        description = {
            "type": "doc", "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "账单基础信息汇报："}]},
                {"type": "codeBlock", "attrs": {"language": "text"},
                 "content": [{"type": "text", "text": summary_text}]}
            ]
        }

        # 2. 构建Issue数据
        rt_id = self._get_request_type_id(project_key)
        if not rt_id:
            return False

        issue_data = {
            "fields": {
                "project": {"key": project_key},
                "summary": summary,
                "description": description,
                "issuetype": {"name": "账单"},
                "customfield_10010": clean_json_value(rt_id),
                "customfield_10483": clean_json_value(self.target_month)
            }
        }

        try:
            # 3. 创建Issue
            create_url = f"https://{self.jira_domain}/rest/api/3/issue"
            resp = requests.post(
                create_url,
                json=issue_data,
                headers={"Content-Type": "application/json"},
                auth=HTTPBasicAuth(self.user_name, self.access_token)
            )
            resp.raise_for_status()
            issue_key = resp.json()["key"]
            self.logger.info(f"Issue创建成功: {issue_key}")

            # 4. 生成临时CSV并上传附件
            # 修正代码：
            # 1. 导入 re 模块（你开头已经导入了）
            # 2. 清洗 map_tag 中的非法路径字符
            safe_map_tag = re.sub(r'[\\/:*?"<>|]', '_', str(map_tag))
            temp_file_name = f"Bill_{bill_account}_{self.target_month}_{safe_map_tag}".replace(" ", "_") + ".csv"
            temp_file_path = os.path.join(self.current_dir, temp_file_name)


            # 保存临时文件（UTF-8 BOM确保Excel兼容）
            target_bill_df.to_csv(temp_file_path, index=False, encoding='utf-8-sig')

            # 调用附件上传函数
            upload_status = upload_jira_attachment(
                self.jira_domain, issue_key, temp_file_path,
                self.user_name, self.access_token
            )

            if upload_status:
                self.logger.info(f"附件上传成功: {temp_file_name} -> {issue_key}")
                os.remove(temp_file_path)  # 清理临时文件
                return True
            else:
                self.logger.error(f"附件上传失败: {temp_file_name} -> {issue_key}")
                os.remove(temp_file_path)
                return False

        except Exception as e:
            self.logger.error(f"Issue创建失败（账号:{bill_account}）: {str(e)}", exc_info=True)
            return False

    def run(self):
        """主执行流程"""
        self.logger.info("=" * 80)
        self.logger.info(f"开始Jira账单同步流程（含附件上传） - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info("=" * 80)

        # 初始化统计
        stats = {
            "success": 0,
            "fail": 0,
            "skipped": 0,
            "total_accounts": 0
        }

        try:
            # 1. 加载数据
            bill_df = self._load_bill_data()
            account_df, _ = self._load_mapping_files()

            # 2. 按账号分组处理
            unique_accounts = bill_df['资源所属账号'].unique()
            stats["total_accounts"] = len(unique_accounts)
            self.logger.info(f"开始处理{len(unique_accounts)}个账号的账单数据")

            for bill_account in unique_accounts:
                # 过滤当前账号的账单
                account_bill_df = bill_df[bill_df['资源所属账号'] == bill_account].copy()
                # 匹配账号映射
                account_matches = account_df[account_df['资源所属账号'] == bill_account]

                if account_matches.empty:
                    self.logger.warning(f"跳过账号: {bill_account} (无映射关系)")
                    stats["skipped"] += len(account_bill_df)
                    continue

                # 处理每个映射记录
                for _, account_row in account_matches.iterrows():
                    map_tag = account_row['标签']
                    project_key = account_row['Project Key']

                    # 按标签过滤账单
                    if map_tag == "":
                        target_bill_df = account_bill_df
                    else:
                        target_bill_df = account_bill_df[account_bill_df['标签'] == map_tag]

                    if target_bill_df.empty:
                        self.logger.info(f"账号{bill_account}标签{map_tag}无匹配账单，跳过")
                        stats["skipped"] += 1
                        continue

                    # 创建Issue并上传附件
                    if self._create_jira_issue(bill_account, map_tag, project_key, target_bill_df):
                        stats["success"] += 1
                    else:
                        stats["fail"] += 1

            # 输出汇总统计
            self.logger.info("\n" + "=" * 80)
            self.logger.info("Jira账单同步汇总统计")
            self.logger.info("=" * 80)
            self.logger.info(f"目标月份: {self.target_month}")
            self.logger.info(f"总处理账号数: {stats['total_accounts']}")
            self.logger.info(f"成功创建Issue数: {stats['success']}")
            self.logger.info(f"创建失败数: {stats['fail']}")
            self.logger.info(f"跳过记录数: {stats['skipped']}")
            self.logger.info("=" * 80)

            # 控制台输出（方便直接查看）
            print("\n" + "=" * 80)
            print("Jira账单同步完成！")
            print("=" * 80)
            print(f"账单月份: {self.target_month}")
            print(f"总账号数: {stats['total_accounts']}")
            print(f"✅ 成功: {stats['success']}")
            print(f"❌ 失败: {stats['fail']}")
            print(f"⏭️  跳过: {stats['skipped']}")
            print("=" * 80)

        except Exception as e:
            self.logger.error(f"同步流程异常终止: {str(e)}", exc_info=True)
            raise


# ===================== 执行入口 =====================
def create_jira_issue():
    """兼容原有函数名的执行入口"""
    try:
        sync = JiraBillSync()
        sync.run()
    except Exception as e:
        logger.error(f"程序执行失败: {str(e)}")
        return


if __name__ == "__main__":
    create_jira_issue()
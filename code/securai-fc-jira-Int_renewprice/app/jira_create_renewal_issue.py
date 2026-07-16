import requests
from requests.auth import HTTPBasicAuth
import configparser
import os
import logging
from datetime import datetime, timedelta
import pandas as pd
import json


# ===================== 全局配置与工具函数 =====================
def clean_json_value(value):
    """清理JSON值，处理空值和特殊类型"""
    if pd.isna(value) or value is None:
        return ""
    return str(value).strip()


def init_logger():
    """初始化日志配置（与之前脚本风格一致）"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.normpath(os.path.join(current_dir, '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'jira_renewal_task.log')

    # 配置日志格式：时间戳+级别+信息，同时输出到文件和控制台
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),  # 写入文件（追加模式）
            logging.StreamHandler()  # 输出到控制台
        ]
    )
    logger = logging.getLogger(__name__)
    # 记录程序启动信息
    logger.info("=" * 80)
    logger.info(f"程序启动：Jira续费任务自动化（时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")
    logger.info("=" * 80)
    return logger


# ===================== 配置与数据处理类 =====================
class JiraRenewalConfig:
    """配置与基础数据管理类（负责配置加载、时间计算、路径生成）"""

    def __init__(self, logger):
        self.logger = logger
        self.current_dir = os.path.dirname(os.path.abspath(__file__))
        # 初始化核心参数（保留时间计算，但不再用于筛选）
        self.target_month = self._get_target_month()  # 上个月（YYYY-MM）
        self.start_date, self.end_date = self._get_current_month_range()  # 本月起止日期
        self.logger.info(f"核心参数初始化完成：目标月份={self.target_month}（仅用于账单文件名）")

    def _get_target_month(self):
        """获取上个月的日期（格式：YYYY-MM）"""
        today = datetime.now()
        first_day_of_current = today.replace(day=1)
        last_day_of_last = first_day_of_current - timedelta(days=1)
        target_month = last_day_of_last.strftime("%Y-%m")
        self.logger.debug(f"自动计算目标月份：{target_month}")
        return target_month

    def _get_current_month_range(self):
        """获取本月的开始和结束日期（仅保留，不再用于筛选）"""
        today = datetime.now()
        start_date = today.replace(day=1).strftime("%Y-%m-%d")
        # 计算本月最后一天
        if today.month == 12:
            next_month = today.replace(year=today.year + 1, month=1, day=1)
        else:
            next_month = today.replace(month=today.month + 1, day=1)
        end_date = (next_month - timedelta(days=1)).strftime("%Y-%m-%d")
        self.logger.debug(f"自动计算本月范围：{start_date} 至 {end_date}（仅保留，不筛选）")
        return start_date, end_date

    def load_jira_config(self):
        """加载Jira配置文件（jira_config.ini）"""
        config_path = os.path.normpath(os.path.join(self.current_dir, '../../jira/config/jira_config.ini'))
        self.logger.info(f"开始加载Jira配置文件：{config_path}")

        if not os.path.exists(config_path):
            self.logger.error(f"Jira配置文件不存在：{config_path}")
            raise FileNotFoundError(f"Jira配置文件不存在：{config_path}")

        config = configparser.ConfigParser()
        config.read(config_path, encoding='utf-8')
        jira_config = {
            'domain': config.get('jira', 'domain'),
            'username': config.get('jira', 'user_name'),
            'token': config.get('jira', 'access_token')
        }
        self.logger.info(f"Jira配置加载成功：域名={jira_config['domain']}，用户名={jira_config['username']}")
        return jira_config

    def get_file_paths(self):
        """生成所有数据文件的路径（新增父/子任务查询文件路径）"""
        data_dir = os.path.normpath(os.path.join(self.current_dir, '../../jira/data'))
        self.logger.info(f"数据文件根目录：{data_dir}")

        file_paths = {
            'renewal_bill': os.path.join(data_dir, f"aliyun_renewal_bill_{self.target_month}.csv"),
            'general_bill': os.path.join(data_dir, f"aliyun_bill_{self.target_month}.csv"),
            'account_mapping': os.path.join(data_dir, "jira_get_account.csv"),
            'request_type': os.path.join(data_dir, "jira_request_type.csv"),
            # 新增：父任务查询文件
            'parent_issues': os.path.join(data_dir, "jira_get_renewal_issues.csv"),
            # 新增：子任务查询文件
            'sub_issues': os.path.join(data_dir, "jira_get_renewal_sbu_issues.csv")
        }

        # 打印文件路径日志
        for file_type, file_path in file_paths.items():
            self.logger.info(f"{file_type}文件路径：{file_path}")

        return file_paths


# ===================== 数据加载与处理类 =====================
class JiraRenewalDataLoader:
    """数据加载与处理类（负责账单读取、筛选、合并、清洗）"""

    def __init__(self, config, logger):
        self.config = config  # JiraRenewalConfig实例
        self.logger = logger
        self.file_paths = config.get_file_paths()

    def load_renewal_bill(self):
        """加载续费账单（移除本月到期筛选条件）"""
        renewal_file = self.file_paths['renewal_bill']
        self.logger.info(f"开始加载续费账单：{renewal_file}（不筛选到期时间）")

        if not os.path.exists(renewal_file):
            self.logger.error(f"续费账单文件缺失：{renewal_file}")
            raise FileNotFoundError(f"续费账单文件缺失：{renewal_file}")

        # 读取CSV（核心修改：移除到期时间筛选）
        renewal_df = pd.read_csv(renewal_file)
        self.logger.info(f"续费账单原始记录数：{len(renewal_df)}（全部保留，不筛选）")

        # 移除到期时间提取和筛选逻辑
        if renewal_df.empty:
            self.logger.info(f"续费账单无记录，程序结束")
            return None
        return renewal_df

    def load_general_bill(self):
        """加载通用账单（补充资源详情和标签）"""
        general_file = self.file_paths['general_bill']
        self.logger.info(f"开始加载通用账单：{general_file}")

        if not os.path.exists(general_file):
            self.logger.warning(f"通用账单文件缺失：{general_file}，将使用续费账单默认值")
            return pd.DataFrame()

        # 读取指定列并去重
        gen_df = pd.read_csv(
            general_file,
            usecols=['资源id', '资源名称', '资源类型', '资源所在地域', '标签']
        ).drop_duplicates(subset=['资源id'])
        self.logger.info(f"通用账单加载成功，去重后记录数：{len(gen_df)}")
        return gen_df

    def load_mapping_tables(self):
        """加载映射表（账号-项目映射、请求类型映射）"""
        # 加载账号-项目映射表
        account_file = self.file_paths['account_mapping']
        self.logger.info(f"开始加载账号-项目映射表：{account_file}")
        if not os.path.exists(account_file):
            self.logger.error(f"账号-项目映射表缺失：{account_file}")
            raise FileNotFoundError(f"账号-项目映射表缺失：{account_file}")
        account_df = pd.read_csv(account_file)
        account_df['标签'] = account_df['标签'].fillna("").astype(str).str.strip()
        self.logger.info(f"账号-项目映射表加载成功，记录数：{len(account_df)}")

        # 加载请求类型映射表
        req_type_file = self.file_paths['request_type']
        self.logger.info(f"开始加载请求类型映射表：{req_type_file}")
        if not os.path.exists(req_type_file):
            self.logger.error(f"请求类型映射表缺失：{req_type_file}")
            raise FileNotFoundError(f"请求类型映射表缺失：{req_type_file}")
        req_type_df = pd.read_csv(req_type_file)
        self.logger.info(f"请求类型映射表加载成功，记录数：{len(req_type_df)}")

        return account_df, req_type_df

    def load_parent_issues_map(self):
        """加载父任务映射表（jira_get_renewal_issues.csv）：账号 → Issue Key"""
        parent_issue_file = self.file_paths['parent_issues']
        self.logger.info(f"开始加载父任务查询文件：{parent_issue_file}")

        parent_issue_map = {}
        if not os.path.exists(parent_issue_file):
            self.logger.warning(f"父任务查询文件缺失：{parent_issue_file}，将创建新父任务")
            return parent_issue_map

        # 读取并构建 账号→Issue Key 映射
        parent_df = pd.read_csv(parent_issue_file)
        # 校验必要列
        if '资源所属账号' not in parent_df.columns or 'Issue Key' not in parent_df.columns:
            self.logger.error(f"父任务查询文件列缺失，需包含'资源所属账号'和'Issue Key'")
            return parent_issue_map

        for _, row in parent_df.iterrows():
            account = clean_json_value(row['资源所属账号'])
            issue_key = clean_json_value(row['Issue Key'])
            if account and issue_key:
                parent_issue_map[account] = issue_key

        self.logger.info(f"父任务映射表加载成功，共{len(parent_issue_map)}个账号的父任务记录")
        return parent_issue_map

    def load_sub_issues_ids(self):
        """加载已存在的子任务资源ID列表（jira_get_renewal_sbu_issues.csv）"""
        sub_issue_file = self.file_paths['sub_issues']
        self.logger.info(f"开始加载子任务查询文件：{sub_issue_file}")

        existing_sub_ids = set()
        if not os.path.exists(sub_issue_file):
            self.logger.warning(f"子任务查询文件缺失：{sub_issue_file}，将创建新子任务")
            return existing_sub_ids

        # 读取并提取已存在的资源ID
        sub_df = pd.read_csv(sub_issue_file)
        if 'ID' not in sub_df.columns:
            self.logger.error(f"子任务查询文件列缺失，需包含'ID'列（资源ID）")
            return existing_sub_ids

        for _, row in sub_df.iterrows():
            res_id = clean_json_value(row['ID'])
            if res_id:
                existing_sub_ids.add(res_id)

        self.logger.info(f"子任务映射表加载成功，共{len(existing_sub_ids)}个已存在的资源ID")
        return existing_sub_ids

    def process_data(self):
        """数据处理主流程：读取、合并、清洗（移除到期时间筛选）"""
        # 1. 加载续费账单（无筛选）
        renewal_df = self.load_renewal_bill()
        if renewal_df is None:
            return None

        # 2. 加载通用账单并合并
        gen_df = self.load_general_bill()
        if not gen_df.empty:
            self.logger.info("开始合并续费账单与通用账单（补充资源详情）")
            final_df = pd.merge(renewal_df, gen_df, on='资源id', how='left')
        else:
            self.logger.info("通用账单为空，使用续费账单原始数据")
            final_df = renewal_df.copy()
            # 初始化空列
            for col in ['资源名称', '资源类型', '资源所在地域', '标签']:
                final_df[col] = ""

        # 3. 数据清洗
        self.logger.info("开始数据清洗（处理空值、去重、格式化）")
        # 确保新增的字段（产品代码、产品类型、地域）即使为空也有默认值
        for col in ['产品代码', '产品类型', '地域']:
            if col not in final_df.columns:
                final_df[col] = ""
        final_df['标签'] = final_df['标签'].fillna("").astype(str).str.strip()
        final_df['资源名称'] = final_df['资源名称'].fillna("").astype(str).str.strip()
        final_df = final_df.reset_index(drop=True)

        self.logger.info(f"数据处理完成，最终有效记录数：{len(final_df)}（全部保留）")
        return final_df


# ===================== Jira操作类 =====================
class JiraOperator:
    """Jira操作类（负责创建Issue、子任务）"""

    def __init__(self, jira_config, logger):
        self.domain = jira_config['domain']
        self.auth = HTTPBasicAuth(jira_config['username'], jira_config['token'])
        self.logger = logger
        self.headers = {
            "Accept": "application/json",
            "Content-Type": "application/json"
        }
        self.logger.info("Jira操作客户端初始化完成")

    def create_issue(self, payload):
        """创建Jira Issue（支持父任务和子任务）"""
        api_url = f"https://{self.domain}/rest/api/3/issue"
        self.logger.debug(f"Jira API请求URL：{api_url}")
        self.logger.debug(f"Jira API请求体：{json.dumps(payload, ensure_ascii=False, indent=2)}")

        try:
            response = requests.post(
                api_url,
                json=payload,
                headers=self.headers,
                auth=self.auth,
                timeout=30  # 设置超时时间
            )

            if response.status_code == 201:
                issue_key = response.json()["key"]
                self.logger.debug(f"Issue创建成功，返回Key：{issue_key}")
                return issue_key
            else:
                self.logger.error(f"Issue创建失败：状态码={response.status_code}，响应内容={response.text}")
                return None
        except requests.exceptions.Timeout:
            self.logger.error("Jira API请求超时（30秒）")
            return None
        except Exception as e:
            self.logger.error(f"Jira API请求异常：{str(e)}", exc_info=True)
            return None


# ===================== 主业务逻辑类 =====================
class JiraRenewalMain:
    """主业务逻辑类（负责分组处理、创建父任务和子任务）"""

    def __init__(self, config, data_loader, jira_operator, logger):
        self.config = config
        self.data_loader = data_loader
        self.jira_operator = jira_operator
        self.logger = logger

    def run(self):
        """执行主业务逻辑"""
        # 1. 加载处理后的数据和映射表
        final_df = self.data_loader.process_data()
        if final_df is None:
            return
        account_df, req_type_df = self.data_loader.load_mapping_tables()
        # 新增：加载父任务映射和已存在的子任务ID
        parent_issue_map = self.data_loader.load_parent_issues_map()
        existing_sub_ids = self.data_loader.load_sub_issues_ids()

        # 2. 重构映射字典：仅保留账号→Project Key（忽略标签）
        self.logger.info("开始构建账号-项目映射字典（仅按账号）")
        mapping_dict = {}
        for _, row in account_df.iterrows():
            account = row['资源所属账号']
            project_key = row['Project Key']
            # 仅按账号映射（覆盖重复账号）
            mapping_dict[account] = project_key
        self.logger.info(f"映射字典构建完成，包含{len(mapping_dict)}条账号映射关系")

        # 3. 修改分组逻辑：仅按账号分组（取消标签维度）
        self.logger.info("开始仅按账号分组处理资源（全部资源）")
        grouped_data = {}
        for _, row in final_df.iterrows():
            account = row['资源所属账号']
            # 仅按账号分组（核心修改：移除标签维度）
            group_key = account
            if group_key not in grouped_data:
                grouped_data[group_key] = []
            grouped_data[group_key].append(row)

        # 转换为可迭代的分组
        grouped = [(k, pd.DataFrame(v)) for k, v in grouped_data.items()]
        self.logger.info(f"共分为{len(grouped)}个账号分组")

        # 4. 处理每个分组（按账号）
        for idx, (account, group) in enumerate(grouped, 1):
            self.logger.info(f"\n{'=' * 50} 处理分组 {idx}/{len(grouped)} {'=' * 50}")
            self.logger.info(f"账号：{account}，资源数量：{len(group)}")

            # A. 匹配Project Key（仅按账号）
            project_key = mapping_dict.get(account)
            if not project_key:
                self.logger.warning(f"未找到账号{account}的Project Key，跳过处理")
                continue

            # B. 获取Request Type ID（课金代行续费）
            match_req = req_type_df[
                (req_type_df['项目键'] == project_key) &
                (req_type_df['Request Type名称'] == "课金代行续费")
                ]
            if match_req.empty:
                self.logger.warning(f"项目{project_key}未配置'课金代行续费'请求类型，跳过处理")
                continue
            req_type_id = match_req['Request TypeID'].values[0]
            self.logger.info(f"项目{project_key}的课金代行续费Request Type ID：{req_type_id}")

            # C. 创建父任务前判断：先查父任务映射表
            parent_key = parent_issue_map.get(account)
            if parent_key:
                self.logger.info(f"账号{account}已存在父任务：{parent_key}，无需创建")
            else:
                # 不存在则创建父任务
                self.logger.info(f"账号{account}无父任务，开始创建")
                parent_summary = f"续费：{account}"  # 仅账号，无标签
                parent_payload = {
                    "fields": {
                        "project": {"key": project_key},
                        "summary": parent_summary,
                        "issuetype": {"name": "续费"},
                        "customfield_10010": clean_json_value(req_type_id),
                        # 新增字段1：customfield_10484（账号）
                        "customfield_10484": clean_json_value(account),
                        # 核心修改：customfield_10493资源所属平台
                        "customfield_10493": {"id": "10375"},
                        "description": {
                            "type": "doc", "version": 1,
                            "content": [{
                                "type": "paragraph",
                                "content": [{"type": "text",
                                             "text": f"{self.config.target_month}阿里云资源续费提醒：\n资源所属账号={account}"}]
                            }]
                        }
                    }
                }
                parent_key = self.jira_operator.create_issue(parent_payload)
                if not parent_key:
                    self.logger.error(f"账号{account}父任务创建失败，跳过子任务处理")
                    continue
                self.logger.info(f"✅ 父任务创建成功：{parent_key}")

            # D. 为每个资源创建子任务（先判断是否已存在）
            self.logger.info(f"开始处理子任务（共{len(group)}个资源）")
            for _, row in group.iterrows():
                res_id = clean_json_value(row['资源id'])
                # 核心修改1：summary改为 续费资源: {产品代码}/{资源id}
                product_code = clean_json_value(row['产品代码'])
                sub_summary = f"续费资源: {product_code}/{res_id}"

                # 子任务创建前判断
                # 条件1：资源ID已存在 → 跳过
                if res_id in existing_sub_ids:
                    self.logger.info(f"   └── ⚠️  子任务已存在（资源ID={res_id}），跳过创建")
                    continue
                # 条件2：父任务ID不存在 → 跳过
                if not parent_key:
                    self.logger.error(f"   └── ❌ 父任务ID不存在，跳过子任务创建：{sub_summary}")
                    continue

                # 不存在则创建子任务
                self.logger.debug(f"创建子任务：{sub_summary}")
                sub_payload = {
                    "fields": {
                        "project": {"key": project_key},
                        "parent": {"key": parent_key},
                        "summary": sub_summary,
                        "issuetype": {"name": "续费明细"},
                        "customfield_10484": clean_json_value(account),  # 账号
                        "customfield_10500": clean_json_value(row['资源到期时间']),  # 到期时间
                        "customfield_10487": clean_json_value(res_id),  # 资源ID
                        "customfield_10486": clean_json_value(row['资源名称']),  # 资源名称
                        # 核心修改2：新增customfield_10918（产品代码）
                        "customfield_10918": clean_json_value(row['产品代码']),
                        # 核心修改3：customfield_10485改为customfield_10920，值为产品类型
                        "customfield_10920": clean_json_value(row['产品类型']),
                        # 核心修改4：customfield_10488的值改为地域（原资源所在地域）
                        "customfield_10488": clean_json_value(row['地域'])
                    }
                }

                sub_key = self.jira_operator.create_issue(sub_payload)
                if sub_key:
                    self.logger.info(f"   └── ✅ 子任务创建成功：{sub_key}")
                else:
                    self.logger.error(f"   └── ❌ 子任务创建失败：{sub_summary}")

        self.logger.info(f"\n{'=' * 80}")
        self.logger.info(f"所有账号分组处理完成！")
        self.logger.info(f"{'=' * 80}")


# ===================== 主程序入口 =====================
def main():
    # 1. 初始化日志
    logger = init_logger()

    try:
        # 2. 初始化核心组件
        config = JiraRenewalConfig(logger)
        data_loader = JiraRenewalDataLoader(config, logger)
        jira_config = config.load_jira_config()
        jira_operator = JiraOperator(jira_config, logger)
        main_logic = JiraRenewalMain(config, data_loader, jira_operator, logger)

        # 3. 执行主业务逻辑
        main_logic.run()

    except Exception as e:
        logger.error(f"程序执行异常：{str(e)}", exc_info=True)
    finally:
        logger.info("=" * 80)
        logger.info(f"程序结束（时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")
        logger.info("=" * 80)


if __name__ == "__main__":
    main()
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
def clean_amount_to_string(value):
    """
    核心修复：专门针对金额字段的格式化转换
    强制将金额转为带有千分位、且保留两位小数的 字符串 (String)
    例如输出 -> "0.00", "47,741.13" (完美契合 Jira 文本字段格式)
    """
    if pd.isna(value) or value is None or value == "":
        return "0.00"

    try:
        # 如果已经是浮点数或整数，直接格式化为带千分位的字符串
        if isinstance(value, (int, float)):
            return "{:,.2f}".format(value)

        if isinstance(value, str):
            # 先剔除原有的千分位和多余空格，转成 float 后再重新格式化
            val = value.replace(',', '').strip()
            if val == "":
                return "0.00"
            return "{:,.2f}".format(float(val))
    except (ValueError, TypeError):
        return "0.00"

    return "0.00"


def clean_query_time(original_time):
    """严格参照要求处理时间格式"""
    if pd.isna(original_time) or original_time is None:
        return None

    original_time = str(original_time).strip()
    if original_time == "":
        return None

    if 'T' in original_time and '+' in original_time:
        return original_time

    try:
        return datetime.strptime(original_time, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%dT%H:%M:%S.000+0800')
    except ValueError:
        try:
            return datetime.strptime(original_time, '%Y-%m-%d %H:%M').strftime('%Y-%m-%dT%H:%M:%S.000+0800')
        except ValueError:
            return original_time


def init_logger():
    """初始化详细日志配置"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.normpath(os.path.join(current_dir, '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'create_confirm_balance_issue.log')

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


logger = init_logger()


# ===================== 核心业务逻辑 =====================
class JiraConfirmBalanceSync:
    def __init__(self):
        self.logger = logger
        self.current_dir = os.path.dirname(os.path.abspath(__file__))
        self.config = self._load_config()
        self.jira_domain = self.config.get('jira', 'domain')
        self.user_name = self.config.get('jira', 'user_name')
        self.access_token = self.config.get('jira', 'access_token')

        self.data_dir = os.path.normpath(os.path.join(self.current_dir, '../../jira/data'))
        self.target_month = self._get_target_month()
        self.renewal_file_path = self._get_renewal_file_path()

    def _load_config(self):
        config_path = os.path.normpath(os.path.join(self.current_dir, '../../jira/config/jira_config.ini'))
        if not os.path.exists(config_path):
            self.logger.error(f"配置文件不存在: {config_path}")
            raise FileNotFoundError(f"配置文件缺失: {config_path}")

        config = configparser.ConfigParser()
        config.read(config_path, encoding='utf-8')
        return config

    def _get_target_month(self):
        today = datetime.now()
        return f"{today.year}-{today.month:02d}"

    def _get_previous_month(self, current_month_str):
        dt = datetime.strptime(current_month_str, "%Y-%m")
        if dt.month == 1:
            return f"{dt.year - 1}-12"
        else:
            return f"{dt.year}-{dt.month - 1:02d}"

    def _get_renewal_file_path(self):
        target_file = f"aliyun_renewal_price_{self.target_month}.csv"
        file_path = os.path.join(self.data_dir, target_file)
        if not os.path.exists(file_path):
            self.logger.error(f"续费文件不存在: {file_path}")
            raise FileNotFoundError(f"续费文件缺失: {file_path}")
        return file_path

    def _load_mapping_files(self):
        account_map_path = os.path.join(self.data_dir, 'jira_get_account.csv')
        if not os.path.exists(account_map_path):
            raise FileNotFoundError(f"账号映射文件缺失")

        account_df = pd.read_csv(account_map_path, encoding='utf-8-sig')
        account_df.columns = account_df.columns.str.strip()
        if '标签' not in account_df.columns:
            account_df['标签'] = ""
        account_df['标签'] = account_df['标签'].fillna("").astype(str)
        self.logger.info(f"成功加载账号映射配置 ({len(account_df)}条记录)")
        return account_df

    def _load_balance_data(self):
        balance_path = os.path.join(self.data_dir, 'balance.csv')
        if not os.path.exists(balance_path):
            self.logger.warning("余额文件 balance.csv 缺失！")
            return pd.DataFrame()

        balance_df = pd.read_csv(balance_path, encoding='utf-8-sig', dtype=str)
        balance_df.columns = balance_df.columns.str.strip()
        self.logger.info(f"成功加载余额数据 ({len(balance_df)}条记录)")
        return balance_df

    def _load_renewal_data(self):
        renewal_df = pd.read_csv(self.renewal_file_path, encoding='utf-8-sig')
        renewal_df.columns = renewal_df.columns.str.strip()

        # 准确计算当前账号的最终价之和 (内部仍用浮点计算总额)
        renewal_df['最终价_Num'] = pd.to_numeric(renewal_df['最终价'].astype(str).str.replace(',', ''),
                                                 errors='coerce').fillna(0.0)
        self.logger.info(f"成功加载本月续费询价清单 ({len(renewal_df)}条资源)")
        return renewal_df

    def _load_bill_data(self):
        """加载上月账单数据，用于统计上月按量付费总额 (customfield_11169)"""
        previous_month = self._get_previous_month(self.target_month)
        bill_file = f"aliyun_bill_{previous_month}.csv"
        bill_path = os.path.join(self.data_dir, bill_file)
        if not os.path.exists(bill_path):
            self.logger.warning(f"上月账单文件缺失: {bill_path}，上月按量总额将使用默认值 0.00")
            return pd.DataFrame()

        bill_df = pd.read_csv(bill_path, encoding='utf-8-sig')
        bill_df.columns = bill_df.columns.str.strip()
        # 数值化消费金额（剔除千分位），用于按账号求和
        bill_df['消费金额_Num'] = pd.to_numeric(
            bill_df['消费金额'].astype(str).str.replace(',', ''), errors='coerce'
        ).fillna(0.0)
        self.logger.info(f"成功加载上月账单数据 ({len(bill_df)}条记录, 账期 {previous_month})")
        return bill_df

    def _get_request_type_id(self, project_key, target_request_type="课金代行确认余额"):
        rt_map_path = os.path.join(self.data_dir, 'jira_request_type.csv')
        if not os.path.exists(rt_map_path):
            return None

        request_type_df = pd.read_csv(rt_map_path, encoding='utf-8-sig')
        request_type_df.columns = request_type_df.columns.str.strip()
        match_rt = request_type_df[
            (request_type_df['项目键'] == project_key) &
            (request_type_df['Request Type名称'] == target_request_type)
            ]
        if match_rt.empty:
            return None
        return str(match_rt['Request TypeID'].values[0])

    def _create_jira_issue(self, bill_account, project_key, target_renewal_df, balance_info, total_renewal_cost, last_month_payg_total=0.0):
        """创建Jira Issue并上传附件，成功则返回 issue_key，失败返回 None"""
        summary = f"【确认余额】{self.target_month} - {bill_account}"
        summary_text = (
            f"确认周期: {self.target_month}\n"
            f"账号: {bill_account}\n"
            f"续费总金额估算: {total_renewal_cost:.2f}\n"
            f"上月按量付费总额: {last_month_payg_total:.2f}\n"
            f"本账号相关续费资源数: {len(target_renewal_df)} 条\n\n"
            f"请查看附件中的 Excel 清单获取当前账号的完整续费资源询价明细。"
        )

        description = {
            "type": "doc", "version": 1,
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "本月续费询价及确认余额信息汇总："}]},
                {"type": "codeBlock", "attrs": {"language": "text"},
                 "content": [{"type": "text", "text": summary_text}]}
            ]
        }

        # ==========================================
        # 🌟 核心：全部转化为带有千分位的标准字符串格式
        # ==========================================
        cash_amount_str = clean_amount_to_string(balance_info.get('available_cash_amount'))
        credit_amount_str = clean_amount_to_string(balance_info.get('available_amount'))
        total_cost_str = clean_amount_to_string(total_renewal_cost)
        last_month_payg_str = clean_amount_to_string(last_month_payg_total)
        currency_val = str(balance_info.get('currency', 'CNY')).strip()

        # 构建基础 payload
        issue_data = {
            "fields": {
                "project": {"key": project_key},
                "summary": summary,
                "description": description,
                "issuetype": {"name": "确认余额"},
                "customfield_10498": cash_amount_str,
                "customfield_10499": credit_amount_str,
                "customfield_11162": total_cost_str,
                "customfield_11169": last_month_payg_str
            }
        }

        # 安全注入币种和时间字段
        if currency_val and currency_val != "None":
            issue_data["fields"]["customfield_10490"] = currency_val

        query_time_val = clean_query_time(balance_info.get('query_time'))
        if query_time_val is not None:
            issue_data["fields"]["customfield_10497"] = query_time_val

        # 绑定门户 Request Type
        rt_id = self._get_request_type_id(project_key, target_request_type="课金代行确认余额")
        if rt_id:
            issue_data["fields"]["customfield_10010"] = rt_id

        self.logger.info(
            f"   ↳ [数据组装完成] 即将发送给 Jira 的 Payload 详情:\n{json.dumps(issue_data, indent=4, ensure_ascii=False)}")

        try:
            self.logger.info(f"[{bill_account}] 正在向 Jira 提交工单创建请求...")
            create_url = f"https://{self.jira_domain}/rest/api/3/issue"
            resp = requests.post(
                create_url,
                json=issue_data,
                headers={"Content-Type": "application/json"},
                auth=HTTPBasicAuth(self.user_name, self.access_token)
            )
            resp.raise_for_status()
            issue_key = resp.json()["key"]
            self.logger.info(f"✅ Issue创建成功: {issue_key}")

            # 上传 Excel 附件
            temp_file_name = f"Renewal_Details_{bill_account}_{self.target_month}_All.xlsx".replace(" ", "_")
            temp_file_path = os.path.join(self.current_dir, temp_file_name)

            export_df = target_renewal_df.drop(columns=['最终价_Num'], errors='ignore')
            export_df.to_excel(temp_file_path, index=False)

            self.logger.info(f"[{bill_account}] 正在向 {issue_key} 上传 Excel 附件: {temp_file_name} ...")
            upload_status = upload_jira_attachment(
                self.jira_domain, issue_key, temp_file_path,
                self.user_name, self.access_token
            )

            if upload_status:
                self.logger.info(f"✅ 附件上传成功: {temp_file_name} -> {issue_key}")
                os.remove(temp_file_path)
                return issue_key
            else:
                self.logger.error(f"❌ 附件上传失败: {temp_file_name} -> {issue_key}")
                os.remove(temp_file_path)
                return None

        except requests.exceptions.HTTPError as e:
            error_msg = e.response.text if e.response is not None else str(e)
            self.logger.error(f"❌ Issue创建被 Jira 拒绝（账号:{bill_account}）。详情: {error_msg}")
            return None
        except Exception as e:
            self.logger.error(f"未知崩溃（账号:{bill_account}）: {str(e)}", exc_info=True)
            return None

    def run(self):
        self.logger.info("=" * 80)
        self.logger.info(f"🚀 开始执行 Jira 确认余额及续费同步流程 - {self.target_month}")
        self.logger.info("=" * 80)

        stats = {"success": 0, "fail": 0, "skipped": 0, "total_accounts": 0}
        created_issues_records = []  # 记录成功创建的 Issue 信息

        try:
            renewal_df = self._load_renewal_data()
            balance_df = self._load_balance_data()
            account_df = self._load_mapping_files()
            bill_df = self._load_bill_data()

            valid_renewal_df = renewal_df.dropna(subset=['资源所属账号'])
            unique_accounts = valid_renewal_df['资源所属账号'].unique()
            stats["total_accounts"] = len(unique_accounts)
            self.logger.info(f"🔍 识别完毕：本月清单中共有 {len(unique_accounts)} 个独立账号需要处理。")

            for account in unique_accounts:
                self.logger.info("-" * 50)
                self.logger.info(f"▶️ 开始处理账号: {account}")

                account_renewal_df = valid_renewal_df[valid_renewal_df['资源所属账号'] == account].copy()
                self.logger.info(f"   ↳ 提取资源数: {len(account_renewal_df)} 条续费记录")

                account_matches = account_df[account_df['资源所属账号'] == account]
                if account_matches.empty:
                    self.logger.warning(f"   ↳ ⏭️ 拦截: 在 mapping 规则中找不到该账号，跳过。")
                    stats["skipped"] += 1
                    continue

                if len(account_matches) > 1:
                    self.logger.warning(
                        f"   ↳ ⚠️ 警告: 账号匹配到 {len(account_matches)} 条映射规则，强制选择第一条进行下发。")

                first_match = account_matches.iloc[0]
                project_key = first_match['Project Key']
                self.logger.info(f"   ↳ 锁定目标项目: Project Key [{project_key}]")

                balance_info = {}
                if not balance_df.empty:
                    match_balance = balance_df[balance_df['role_session_name'] == account]
                    if not match_balance.empty:
                        balance_info = match_balance.iloc[0].dropna().to_dict()
                        self.logger.info(
                            f"   ↳ [余额提取成功] 匹配到的原始 CSV 数据:\n{json.dumps(balance_info, indent=4, ensure_ascii=False)}")
                    else:
                        self.logger.warning(f"   ↳ ⚠️ 警告: 未在余额表找到账号 {account} 的信息，将使用默认值 0.00")

                total_renewal_cost = account_renewal_df['最终价_Num'].sum()
                self.logger.info(f"   ↳ [续费计算成功] 账号总续费金额核算结果: {total_renewal_cost:.2f}")

                # 上月按量付费总额 (customfield_11169)
                if not bill_df.empty:
                    payg_df = bill_df[
                        (bill_df['资源所属账号'] == account) &
                        (bill_df['资源付费方式'] == '按量付费')
                    ]
                    last_month_payg_total = payg_df['消费金额_Num'].sum()
                else:
                    last_month_payg_total = 0.0
                self.logger.info(f"   ↳ [上月按量统计] 账号上月按量付费总额: {last_month_payg_total:.2f}")

                # 提取返回的 issue_key
                issue_key = self._create_jira_issue(
                        bill_account=account,
                        project_key=project_key,
                        target_renewal_df=account_renewal_df,
                        balance_info=balance_info,
                        total_renewal_cost=total_renewal_cost,
                        last_month_payg_total=last_month_payg_total
                )

                if issue_key:
                    stats["success"] += 1
                    # 成功后加入输出记录表
                    created_issues_records.append({
                        "Issue Key": issue_key,
                        "Project Key": project_key,
                        "资源所属账号": account,
                        "总续费费用_月前": f"{total_renewal_cost:.2f}"
                    })
                else:
                    stats["fail"] += 1

            self.logger.info("\n" + "=" * 80)
            self.logger.info("📊 流程执行汇总")
            self.logger.info("=" * 80)
            self.logger.info(f"总处理账号数 : {stats['total_accounts']}")
            self.logger.info(f"✅ 成功下发数 : {stats['success']}")
            self.logger.info(f"❌ 失败报错数 : {stats['fail']}")
            self.logger.info(f"⏭️ 忽略跳过数 : {stats['skipped']}")
            self.logger.info("=" * 80)

            # ==========================================
            # 🌟 新增：输出 CSV 记录文件
            # ==========================================
            if created_issues_records:
                output_csv_path = os.path.join(self.data_dir, 'jira_get_renewal_price_issues.csv')
                df_records = pd.DataFrame(created_issues_records)
                # 按照要求的列顺序排列输出
                df_records = df_records[['Issue Key', 'Project Key', '资源所属账号', '总续费费用_月前']]
                df_records.to_csv(output_csv_path, index=False, encoding='utf-8-sig')
                self.logger.info(f"💾 已成功将创建的工单记录导出至: {output_csv_path} (共 {len(created_issues_records)} 条记录)")
            else:
                self.logger.info("ℹ️ 本次运行未能成功创建任何工单，跳过 CSV 记录导出。")

            print("\nJira确认余额及续费同步流程执行完毕！详细记录请看日志。")

        except Exception as e:
            self.logger.error(f"同步流程发生重大异常终止: {str(e)}", exc_info=True)
            raise


if __name__ == "__main__":
    sync = JiraConfirmBalanceSync()
    sync.run()
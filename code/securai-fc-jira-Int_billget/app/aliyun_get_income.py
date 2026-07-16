from alibabacloud_sts20150401.client import Client as StsClient
from alibabacloud_sts20150401.models import AssumeRoleRequest
import configparser
import os
import sys
from typing import List, Dict, Any
from alibabacloud_bssopenapi20171214.client import Client as BssOpenApi20171214Client
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_bssopenapi20171214 import models as bss_open_api_20171214_models
from alibabacloud_tea_util import models as util_models
import logging
import csv
from datetime import datetime, timedelta


# ===================== 日志初始化配置 =====================
def init_logger():
    """初始化日志，输出到 ../../jira/log/reset_income.log（独立日志文件）"""
    log_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'reset_income.log')

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
# =============================================================

# 数据文件路径配置
DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/data'))
OUTPUT_FILE_PATH = ''


class AliyunMultiRoleIncomeQuery:
    @staticmethod
    def get_all_role_sections() -> List[str]:
        """自动识别配置文件中所有aliyun-开头的角色分组"""
        config_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/config/aliyun_config.ini'))
        config = configparser.ConfigParser()

        if not os.path.exists(config_path):
            logger.error(f"配置文件不存在：{config_path}")
            raise FileNotFoundError(f"配置文件不存在：{config_path}")
        config.read(config_path, encoding='utf-8')
        logger.info(f"已加载配置文件：{config_path}")

        role_sections = [section for section in config.sections() if section.startswith('aliyun-')]
        if not role_sections:
            logger.error("配置文件中未找到任何aliyun-开头的角色分组")
            raise ValueError("配置文件中未找到任何aliyun-开头的角色分组")
        logger.info(f"识别到{len(role_sections)}个角色分组：{role_sections}")
        return role_sections

    @staticmethod
    def get_master_credentials() -> tuple[str, str]:
        """从配置文件读取主账号AK（用于STS角色扮演）"""
        config_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/config/aliyun_config.ini'))
        config = configparser.ConfigParser()
        config.read(config_path, encoding='utf-8')

        try:
            master_ak_id = config.get('aliyun', 'access_key_id')
            master_ak_secret = config.get('aliyun', 'access_key_secret')
        except (configparser.NoSectionError, configparser.NoOptionError) as e:
            logger.error(f"主账号配置缺失：{e}")
            raise ValueError(f"主账号配置缺失：{e}")

        if not all([master_ak_id, master_ak_secret]):
            logger.error("主账号access_key_id和access_key_secret不能为空")
            raise ValueError("主账号access_key_id和access_key_secret不能为空")
        logger.info("主账号凭证读取成功")
        return master_ak_id, master_ak_secret

    @staticmethod
    def create_client_by_role(role_section: str) -> BssOpenApi20171214Client:
        """根据指定角色分组创建BSS OpenAPI客户端"""
        logger.info(f"\n开始初始化角色{role_section}的客户端...")
        master_ak_id, master_ak_secret = AliyunMultiRoleIncomeQuery.get_master_credentials()

        config_path = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/config/aliyun_config.ini'))
        config = configparser.ConfigParser()
        config.read(config_path, encoding='utf-8')

        try:
            role_arn = config.get(role_section, 'role_arn')
            role_session_name = config.get(role_section, 'role_session_name')
        except (configparser.NoSectionError, configparser.NoOptionError) as e:
            logger.error(f"角色{role_section}配置错误：{e}")
            raise ValueError(f"角色{role_section}配置错误：{e}")

        if not all([role_arn, role_session_name]):
            logger.error(f"角色{role_section}的role_arn和role_session_name不能为空")
            raise ValueError(f"角色{role_section}的role_arn和role_session_name不能为空")
        logger.info(f"角色{role_section}配置读取成功（role_arn：{role_arn[:20]}...）")

        # 初始化STS客户端
        sts_config = open_api_models.Config(
            access_key_id=master_ak_id,
            access_key_secret=master_ak_secret
        )
        sts_config.endpoint = 'sts.aliyuncs.com'
        sts_client = StsClient(sts_config)
        logger.info("STS客户端初始化完成")

        # 申请临时访问凭证
        assume_role_request = AssumeRoleRequest(
            role_arn=role_arn,
            role_session_name=role_session_name,
            duration_seconds=3600
        )
        logger.info(f"角色{role_section}：发起临时凭证申请...")
        response = sts_client.assume_role(assume_role_request)

        if not response.body.credentials:
            logger.error(f"角色{role_section}获取临时凭证失败")
            raise Exception(f"角色{role_section}获取临时凭证失败")

        # 用临时凭证创建BSS客户端
        credentials = response.body.credentials
        bss_config = open_api_models.Config(
            access_key_id=credentials.access_key_id,
            access_key_secret=credentials.access_key_secret,
            security_token=credentials.security_token
        )
        bss_config.endpoint = "business.ap-southeast-1.aliyuncs.com"
        logger.info(f"✅ 角色{role_section}：临时凭证获取成功，BSS客户端创建完成")
        return BssOpenApi20171214Client(bss_config)

    @staticmethod
    def get_last_month_date_range() -> tuple[str, str, str]:
        """自动获取上一个月的日期范围（开始/结束日期）和账期（YYYY-MM）"""
        today = datetime.today()
        first_day_of_current = today.replace(day=1)
        last_day_of_last = first_day_of_current - timedelta(days=1)
        first_day_of_last = last_day_of_last.replace(day=1)

        create_time_start = first_day_of_last.strftime("%Y-%m-%d")
        create_time_end = last_day_of_last.strftime("%Y-%m-%d")
        billing_cycle = last_day_of_last.strftime("%Y-%m")

        logger.info(f"自动获取上一个月信息：")
        logger.info(f"  开始日期：{create_time_start}")
        logger.info(f"  结束日期：{create_time_end}")
        logger.info(f"  账期：{billing_cycle}")
        return create_time_start, create_time_end, billing_cycle

    @staticmethod
    def format_transaction_time(utc_time_str: str) -> str:
        """将UTC时间（如2025-09-24T09:13:31Z）格式化为本地时间（YYYY-MM-DD HH:MM:SS）"""
        if not utc_time_str or utc_time_str.endswith('Z'):
            utc_time_str = utc_time_str[:-1] if utc_time_str else ''

        try:
            # 解析UTC时间
            utc_datetime = datetime.fromisoformat(utc_time_str)
            # 转换为本地时间（自动处理时区偏移）
            local_datetime = utc_datetime.astimezone()
            # 格式化输出
            return local_datetime.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            # 解析失败时返回原始值
            return utc_time_str or '未知时间'

    @staticmethod
    def process_single_role_income(response_body: Dict[str, Any], billing_cycle: str) -> List[Dict[str, Any]]:
        """处理单个角色的收入数据，过滤Refund类型，返回格式化明细列表"""
        logger.info("开始处理收入数据...")
        if not response_body.get('Success') or not response_body.get('Data'):
            logger.warning("无有效收入数据返回")
            return []

        data = response_body['Data']
        account_name = data.get('AccountName', '未知账号')

        # 提取收入明细数据
        income_items = data.get('AccountTransactionsList', {}).get('AccountTransactionsList', [])
        income_items = income_items if isinstance(income_items, list) else [income_items] if income_items else []
        logger.info(f"原始收入记录数：{len(income_items)}")

        # 格式化收入明细，过滤交易类型为Refund的记录
        processed_details = []
        refund_count = 0  # 统计过滤的退款记录数
        for item in income_items:
            transaction_type = item.get('TransactionType', '未知')
            # 跳过交易类型为Refund的记录
            if transaction_type == 'Refund':
                refund_count += 1
                continue

            item_billing_cycle = item.get('BillingCycle', billing_cycle)
            transaction_time = AliyunMultiRoleIncomeQuery.format_transaction_time(item.get('TransactionTime', ''))

            detail = {
                '资源所属账号': account_name,  # 修改：账号 -> 资源所属账号
                '账期': item_billing_cycle,
                '交易时间': transaction_time,
                '交易渠道': item.get('TransactionChannel', '未知'),
                '消费金额': round(float(item.get('Amount', 0)), 5),  # 修改：金额 -> 消费金额
                '交易类型': transaction_type,
                '资金形式': item.get('FundType', '未知')
            }
            processed_details.append(detail)

        logger.info(f"处理完成：有效收入明细{len(processed_details)}条，过滤Refund类型记录{refund_count}条")
        return processed_details

    @staticmethod
    def init_output_file():
        """初始化输出CSV文件（覆盖模式）"""
        os.makedirs(DATA_DIR, exist_ok=True)
        logger.info(f"数据目录：{DATA_DIR}（已确保存在）")

        # 输出字段顺序（修改列名）
        output_fields = [
            '资源所属账号',  # 修改：账号 -> 资源所属账号
            '账期',
            '交易时间',
            '交易渠道',
            '消费金额',  # 修改：金额 -> 消费金额
            '交易类型',
            '资金形式'
        ]

        # 覆盖创建文件并写入表头
        with open(OUTPUT_FILE_PATH, 'w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=output_fields)
            writer.writeheader()
        logger.info(f"✅ 输出文件初始化完成：{OUTPUT_FILE_PATH}")

    @staticmethod
    def append_to_output_file(detail_list: List[Dict[str, Any]]):
        """将收入明细数据追加到输出CSV文件"""
        if not detail_list:
            logger.warning("无有效收入数据可写入文件")
            return

        # 输出字段顺序（修改列名）
        output_fields = [
            '资源所属账号',  # 修改：账号 -> 资源所属账号
            '账期',
            '交易时间',
            '交易渠道',
            '消费金额',  # 修改：金额 -> 消费金额
            '交易类型',
            '资金形式'
        ]

        with open(OUTPUT_FILE_PATH, 'a', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=output_fields)
            writer.writerows(detail_list)
        logger.info(f"✅ 成功写入{len(detail_list)}条收入明细到文件")

    @staticmethod
    def query_single_role(role_section: str, create_time_start: str, create_time_end: str, billing_cycle: str) -> List[
        Dict[str, Any]]:
        """查询单个角色的收入数据，返回明细列表"""
        try:
            client = AliyunMultiRoleIncomeQuery.create_client_by_role(role_section)
            # 构造收入查询请求
            query_request = bss_open_api_20171214_models.QueryAccountTransactionsRequest(
                create_time_start=create_time_start,
                create_time_end=create_time_end,
                transaction_flow='Income',
                page_size=100
            )
            runtime = util_models.RuntimeOptions()
            logger.info(f"角色{role_section}：发起收入查询请求")
            logger.info(f"  时间范围：{create_time_start} 至 {create_time_end}")
            response = client.query_account_transactions_with_options(query_request, runtime)
            response_body = response.body.to_map() if hasattr(response.body, 'to_map') else dict(response.body)
            logger.info(f"角色{role_section}：收入查询响应成功")

            return AliyunMultiRoleIncomeQuery.process_single_role_income(response_body, billing_cycle)
        except Exception as e:
            error_msg = str(e)
            logger.error(f"❌ 角色{role_section}查询失败：{error_msg}", exc_info=True)
            return []

    @staticmethod
    def main(args: List[str]) -> None:
        # 日志记录程序启动
        logger.info("=" * 80)
        logger.info(
            f"程序启动：阿里云多角色收入明细查询工具（过滤Refund类型）（时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")
        logger.info("=" * 80)

        # 解析命令行参数
        import argparse
        parser = argparse.ArgumentParser(
            description='阿里云多角色收入明细查询工具（QueryAccountTransactions接口，过滤Refund类型）')
        parser.add_argument('-r', '--role', type=str,
                            help='指定单个角色分组名（如 aliyun-skyarch.cn@skyarch），不指定则查询所有角色')
        parser.add_argument('-s', '--start', type=str, help='查询开始日期（格式：YYYY-MM-DD），不指定则用上月第一天')
        parser.add_argument('-e', '--end', type=str, help='查询结束日期（格式：YYYY-MM-DD），不指定则用上月最后一天')
        parsed_args = parser.parse_args(args)
        logger.info(
            f"命令行参数：role={parsed_args.role or '未指定'}, start={parsed_args.start or '未指定'}, end={parsed_args.end or '未指定'}")

        try:
            # 确定查询的角色列表
            if parsed_args.role:
                role_sections = [parsed_args.role]
                logger.info(f"指定查询单个角色：{role_sections[0]}")
            else:
                role_sections = AliyunMultiRoleIncomeQuery.get_all_role_sections()
                logger.info(f"未指定角色，将查询所有{len(role_sections)}个角色")

            # 确定查询时间范围和账期
            if parsed_args.start and parsed_args.end:
                create_time_start = parsed_args.start
                create_time_end = parsed_args.end
                billing_cycle = create_time_start[:7]
                logger.info(f"使用指定时间范围：{create_time_start} 至 {create_time_end}")
            else:
                create_time_start, create_time_end, billing_cycle = AliyunMultiRoleIncomeQuery.get_last_month_date_range()
            logger.info(f"最终查询参数：")
            logger.info(f"  账期：{billing_cycle}")
            logger.info(f"  时间范围：{create_time_start} - {create_time_end}")

            # 输出文件名格式：aliyun_income_YYYY-MM.csv
            output_filename = f'aliyun_income_{billing_cycle}.csv'
            global OUTPUT_FILE_PATH
            OUTPUT_FILE_PATH = os.path.join(DATA_DIR, output_filename)

            # 初始化输出文件
            AliyunMultiRoleIncomeQuery.init_output_file()

            # 批量查询所有角色并写入文件
            total_detail_count = 0
            total_refund_count = 0  # 统计所有角色的退款过滤数
            for role in role_sections:
                logger.info(f"\n" + "-" * 50)
                logger.info(f"开始查询角色：{role}")
                logger.info("-" * 50)
                details = AliyunMultiRoleIncomeQuery.query_single_role(role, create_time_start, create_time_end,
                                                                       billing_cycle)
                # 计算当前角色的退款过滤数（原始记录数 - 有效明细数）
                original_count = len([item for item in
                                      AliyunMultiRoleIncomeQuery.query_single_role(role, create_time_start,
                                                                                   create_time_end, billing_cycle)])
                refund_count = original_count - len(details)
                total_refund_count += refund_count

                if details:
                    AliyunMultiRoleIncomeQuery.append_to_output_file(details)
                    total_detail_count += len(details)
                    logger.info(f"角色{role}：{len(details)}条有效明细已写入文件，过滤Refund记录{refund_count}条")
                else:
                    logger.info(f"角色{role}：无有效收入明细可写入文件，过滤Refund记录{refund_count}条")

            # 输出最终统计
            logger.info(f"\n" + "=" * 80)
            logger.info(f"查询完成！")
            logger.info(f"账期：{billing_cycle}")
            logger.info(f"查询角色数：{len(role_sections)}")
            logger.info(f"总收入明细数：{total_detail_count}")
            logger.info(f"输出文件：{OUTPUT_FILE_PATH}")
            logger.info("=" * 80 + "\n")

            print(f"\n" + "=" * 80)
            print(f"查询完成！")
            print(f"=" * 80)
            print(f"账期：{billing_cycle}")
            print(f"查询角色数：{len(role_sections)}")
            print(f"总收入明细数：{total_detail_count}")
            print(f"输出文件：{OUTPUT_FILE_PATH}")
            print(f"=" * 80)

        except Exception as e:
            logger.error(f"❌ 程序执行失败：{str(e)}", exc_info=True)
            sys.exit(1)


if __name__ == '__main__':
    AliyunMultiRoleIncomeQuery.main(sys.argv[1:])
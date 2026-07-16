from alibabacloud_sts20150401.client import Client as StsClient
from alibabacloud_sts20150401.models import AssumeRoleRequest
from alibabacloud_bssopenapi20171214.client import Client as BssOpenApi20171214Client
from alibabacloud_tea_openapi import models as open_api_models
import configparser
import os
import argparse
import csv
import logging
import datetime
import sys
from typing import List, Dict, Any
from prettytable import PrettyTable  # 用于表格展示
from alibabacloud_tea_util import models as util_models  # 保持统一性

# 确保data目录存在
DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/data'))
# 生成带日期的文件名（如balance-20251113.csv）
current_date = datetime.datetime.now().strftime('%Y%m%d')
BALANCE_FILE = os.path.join(DATA_DIR, f'balance.csv')


# ===================== 日志初始化配置 =====================
def init_logger():
    """初始化日志，输出到 ../../jira/log/reset_available.log 和控制台"""
    log_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'reset_available.log')

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


# 初始化日志实例
logger = init_logger()


# =============================================================

class AliyunBalanceQuery:
    """包含了身份验证、客户端创建和余额查询逻辑的类"""

    CONFIG_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/config/aliyun_config.ini'))

    @staticmethod
    def get_all_role_sections() -> List[str]:
        """自动识别配置文件中所有aliyun-开头的角色分组"""
        config = configparser.ConfigParser()
        if not os.path.exists(AliyunBalanceQuery.CONFIG_PATH):
            logger.error(f"配置文件不存在：{AliyunBalanceQuery.CONFIG_PATH}")
            raise FileNotFoundError(f"配置文件不存在：{AliyunBalanceQuery.CONFIG_PATH}")

        config.read(AliyunBalanceQuery.CONFIG_PATH, encoding='utf-8')
        role_sections = [section for section in config.sections() if section.startswith('aliyun-')]

        if not role_sections:
            logger.error("配置文件中未找到任何aliyun-开头的角色分组")
            raise ValueError("配置文件中未找到任何aliyun-开头的角色分组")

        return role_sections

    @staticmethod
    def get_master_credentials() -> tuple[str, str]:
        """从配置文件读取主账号AK（用于STS角色扮演）"""
        config = configparser.ConfigParser()
        config.read(AliyunBalanceQuery.CONFIG_PATH, encoding='utf-8')

        try:
            master_ak_id = config.get('aliyun', 'access_key_id')
            master_ak_secret = config.get('aliyun', 'access_key_secret')
        except (configparser.NoSectionError, configparser.NoOptionError) as e:
            logger.error(f"主账号配置缺失：{e}")
            raise ValueError(f"主账号配置缺失：{e}")

        if not all([master_ak_id, master_ak_secret]):
            logger.error("主账号access_key_id和access_key_secret不能为空")
            raise ValueError("主账号access_key_id和access_key_secret不能为空")

        return master_ak_id, master_ak_secret

    @staticmethod
    def create_client_by_role(role_section: str) -> BssOpenApi20171214Client:
        """根据指定角色分组创建BSS OpenAPI客户端（使用STS角色扮演）"""
        logger.info(f"\n开始初始化角色{role_section}的客户端...")
        master_ak_id, master_ak_secret = AliyunBalanceQuery.get_master_credentials()

        config = configparser.ConfigParser()
        config.read(AliyunBalanceQuery.CONFIG_PATH, encoding='utf-8')

        try:
            role_arn = config.get(role_section, 'role_arn')
            role_session_name = config.get(role_section, 'role_session_name')
        except (configparser.NoSectionError, configparser.NoOptionError) as e:
            logger.error(f"角色{role_section}配置错误：{e}")
            raise ValueError(f"角色{role_section}配置错误：{e}")

        if not all([role_arn, role_session_name]):
            logger.error(f"角色{role_section}的role_arn和role_session_name不能为空")
            raise ValueError(f"角色{role_section}的role_arn和role_session_name不能为空")

        # 初始化STS客户端
        sts_config = open_api_models.Config(
            access_key_id=master_ak_id,
            access_key_secret=master_ak_secret
        )
        sts_config.endpoint = 'sts.aliyuncs.com'
        sts_client = StsClient(sts_config)

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
        bss_config.endpoint = 'business.ap-southeast-1.aliyuncs.com'

        # BSS OpenAPI 客户端初始化 (用于查询余额)
        client = BssOpenApi20171214Client(bss_config)
        logger.info(f"✅ 角色{role_section}：临时凭证获取成功，BSS客户端创建完成")
        return client, role_session_name  # 返回客户端和会话名


def init_data_file():
    """初始化每日balance文件（如果不存在则创建并写入表头）"""
    os.makedirs(DATA_DIR, exist_ok=True)
    try:
        # 覆盖创建文件并写入表头
        with open(BALANCE_FILE, 'w', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            # 表头：查询时间、角色会话名、可用余额、可用现金金额、货币单位
            writer.writerow(
                ['query_time', 'role_session_name', 'available_amount', 'available_cash_amount', 'currency'])
        logger.info(f"初始化balance文件成功：{BALANCE_FILE}")
    except Exception as e:
        logger.error(f"初始化balance文件失败：{str(e)}")
        raise


def write_balance_data(data: dict):
    """将查询结果写入每日balance文件（追加模式）"""
    try:
        with open(BALANCE_FILE, 'a', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                data['query_time'],
                data['role_session_name'],
                data['available_amount'],
                data['available_cash_amount'],
                data['currency']
            ])
        logger.info(f"写入数据成功：{data}")
    except Exception as e:
        logger.error(f"写入数据失败（数据：{data}）：{str(e)}")


def read_balance_data():
    """读取当前日期的balance文件中的历史数据并以表格形式展示"""
    if not os.path.exists(BALANCE_FILE):
        logger.warning(f"暂无今日数据（文件不存在：{BALANCE_FILE}）")
        print(f"暂无今日数据（{BALANCE_FILE}不存在）")
        return

    table = PrettyTable()
    table.field_names = ['查询时间', '角色会话名', '可用余额', '可用现金金额', '货币单位']
    table.align = 'l'

    try:
        with open(BALANCE_FILE, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            next(reader)  # 跳过表头
            row_count = 0
            for row in reader:
                if len(row) == len(table.field_names):  # 确保行数据完整
                    table.add_row(row)
                    row_count += 1
                else:
                    logger.warning(f"跳过不完整数据行: {row}")

        logger.info(f"成功读取{row_count}条历史数据（文件：{BALANCE_FILE}）")
        print(f"\n=== {current_date} 余额历史数据 ===")
        print(table)
    except Exception as e:
        logger.error(f"读取历史数据失败：{str(e)}")
        print(f"读取历史数据失败：{str(e)}")


def query_account_balance(role_section: str = None, show_history: bool = False) -> None:
    """
    查询阿里云账号可用余额（支持表格输出和数据持久化）
    :param role_section: 指定角色分组名
    :param show_history: 是否显示历史数据
    """
    logger.info("=" * 80)
    logger.info(f"🚀 开始执行阿里云余额查询（时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")
    logger.info(f"指定角色分组：{role_section or '全部'} | 是否显示历史数据：{show_history}")
    logger.info("=" * 80)

    # 初始化数据文件（每日文件，覆盖写入表头）
    init_data_file()

    # 如果需要显示历史数据，先读取并展示
    if show_history:
        logger.info("开始读取历史数据...")
        read_balance_data()
        if role_section is None:
            # 如果只指定了 -H 且没有指定角色，则只显示历史数据并退出
            logger.info("仅展示历史数据，未执行新查询，程序结束")
            sys.exit(0)  # 退出程序

    # 获取要查询的角色列表
    try:
        if role_section:
            role_sections = [role_section]
        else:
            role_sections = AliyunBalanceQuery.get_all_role_sections()
            logger.info(f"检测到 {len(role_sections)} 个角色，开始批量查询...")
    except Exception as e:
        logger.error(f"获取角色列表失败：{str(e)}")
        print(f"❌ 获取角色列表失败：{str(e)}")
        return

    # 用于存储本次查询结果（表格展示用）
    result_table = PrettyTable()
    result_table.field_names = ['查询时间', '角色会话名', '可用余额', '可用现金金额', '货币单位']
    result_table.align = 'l'

    for section in role_sections:
        logger.info(f"\n--- 开始查询角色分组：{section} ---")
        role_session_name = "N/A"  # 预设会话名
        try:
            # 1. 获取客户端 (使用 STS 逻辑)
            client, role_session_name = AliyunBalanceQuery.create_client_by_role(role_section=section)

            # 2. 记录时间
            query_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            # 3. 调用API
            logger.info(f"调用阿里云余额查询API（会话：{role_session_name}）")
            # QueryAccountBalance 不需要 Request object，但需要 RuntimeOptions
            runtime = util_models.RuntimeOptions()
            response = client.query_account_balance_with_options(runtime)

            # 4. 处理结果
            if response.body.success:
                data = response.body.data
                available_amount = data.available_amount
                available_cash_amount = data.available_cash_amount
                currency = data.currency

                result = {
                    'query_time': query_time,
                    'role_session_name': role_session_name,
                    'available_amount': available_amount,
                    'available_cash_amount': available_cash_amount,
                    'currency': currency
                }
                result_table.add_row([
                    query_time, role_session_name, available_amount, available_cash_amount, currency
                ])
                # 写入文件
                write_balance_data(result)
                logger.info(f"✅ 查询成功（会话：{role_session_name}）- 可用余额：{available_amount} {currency}")
            else:
                err_msg = f"查询失败（错误码：{response.body.code}）"
                result_table.add_row([
                    query_time, role_session_name, err_msg, "", response.body.code or "N/A"  # 错误码作为货币单位占位
                ])
                logger.error(f"❌ {err_msg}（会话：{role_session_name}，响应详情：{response.body.message}）")

        except Exception as e:
            err_detail = str(e)
            # 检查是否为 STS 错误，如果是，使用 section 代替 session_name
            if 'AssumeRole' in err_detail or 'access_key_id' in err_detail:
                session_display = section
            else:
                session_display = role_session_name  # 使用已知的会话名

            result_table.add_row([
                datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                session_display,
                "查询/认证异常",
                "",
                err_detail[:30] + "..."
            ])
            logger.error(f"❌ 角色分组{section}查询异常：{err_detail}", exc_info=True)

    # 展示本次查询结果表格
    logger.info("\n本次查询完成，展示结果表格")
    print("\n" + "=" * 80)
    print("✨ 本次查询结果（所有角色）✨")
    print(result_table)
    print("=" * 80 + "\n")
    logger.info("=" * 80 + "\n")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='阿里云余额查询工具（STS认证+表格输出+数据持久化）')
    parser.add_argument('-r', '--role', type=str,
                        help='指定单个角色分组名（如 aliyun-jira），不指定则查询所有 aliyn- 开头的分组')
    parser.add_argument('-H', '--history', action='store_true',
                        help='显示今日历史查询数据，如果同时指定 -r 则先显示历史再查询新数据')
    args = parser.parse_args()

    query_account_balance(
        role_section=args.role,
        show_history=args.history
    )
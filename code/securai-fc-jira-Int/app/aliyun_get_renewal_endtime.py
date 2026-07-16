from alibabacloud_sts20150401.client import Client as StsClient
from alibabacloud_sts20150401.models import AssumeRoleRequest
import configparser
import os
import sys
import re
from typing import List, Dict, Any
from alibabacloud_bssopenapi20171214.client import Client as BssOpenApi20171214Client
from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_bssopenapi20171214 import models as bss_open_api_20171214_models
from alibabacloud_tea_util import models as util_models
import logging
import csv
import argparse
from datetime import datetime, timedelta

# ===================== 全局配置与常量定义 =====================
# 数据目录配置
DATA_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/data'))
# 日志文件路径
LOG_FILE = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/log/create_renewal_issue.log'))
# 全局输出文件路径（运行时赋值）
OUTPUT_FILE_PATH = ''


# ===================== 日志初始化配置 =====================
def init_logger() -> logging.Logger:
    """初始化日志配置，输出到文件和控制台"""
    # 创建日志目录
    log_dir = os.path.dirname(LOG_FILE)
    os.makedirs(log_dir, exist_ok=True)

    # 配置日志格式
    log_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

    # 文件处理器（追加模式）
    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
    file_handler.setFormatter(log_format)

    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(log_format)

    # 初始化logger
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger


# 初始化日志实例
logger = init_logger()


# ===================== 核心业务类 =====================
class AliyunMultiRoleRenewalQuery:
    """阿里云多角色续费实例查询工具类"""

    @staticmethod
    def _get_config_path() -> str:
        """获取配置文件路径（封装路径逻辑，便于维护）"""
        return os.path.normpath(os.path.join(os.path.dirname(__file__), '../../jira/config/aliyun_config.ini'))

    @staticmethod
    def sanitize_session_name(name: str) -> str:
        """
        清洗 RoleSessionName 以符合阿里云规范：
        1. 只允许英文字母、数字、句点(.)、下划线(_)、短划线(-)
        2. 长度 2-64 个字符
        """
        # 替换非合法字符为下划线
        sanitized = re.sub(r'[^a-zA-Z0-9.\-_]', '_', name)

        # 确保长度符合要求
        if len(sanitized) < 2:
            sanitized = f"STS_{sanitized}" if sanitized else "STS_Session"

        # 限制最大长度为64
        return sanitized[:64]

    @staticmethod
    def get_all_role_sections() -> List[str]:
        """获取配置文件中所有aliyun-开头的角色分组"""
        config_path = AliyunMultiRoleRenewalQuery._get_config_path()

        # 检查配置文件是否存在
        if not os.path.exists(config_path):
            logger.error(f"配置文件不存在：{config_path}")
            raise FileNotFoundError(f"配置文件不存在：{config_path}")

        # 读取配置文件
        config = configparser.ConfigParser()
        config.read(config_path, encoding='utf-8')

        # 筛选角色分组
        role_sections = [section for section in config.sections() if section.startswith('aliyun-')]

        if not role_sections:
            logger.warning("配置文件中未找到任何aliyun-开头的角色分组")
        else:
            logger.info(f"识别到{len(role_sections)}个角色分组：{role_sections}")

        return role_sections

    @staticmethod
    def get_master_credentials() -> tuple[str, str]:
        """从配置文件读取主账号AK/SK（用于STS角色扮演）"""
        config_path = AliyunMultiRoleRenewalQuery._get_config_path()
        config = configparser.ConfigParser()
        config.read(config_path, encoding='utf-8')

        try:
            master_ak_id = config.get('aliyun', 'access_key_id')
            master_ak_secret = config.get('aliyun', 'access_key_secret')
        except (configparser.NoSectionError, configparser.NoOptionError) as e:
            logger.error(f"主账号配置缺失：{str(e)}")
            raise ValueError(f"主账号配置缺失：{str(e)}")

        if not all([master_ak_id, master_ak_secret]):
            logger.error("主账号access_key_id和access_key_secret不能为空")
            raise ValueError("主账号access_key_id和access_key_secret不能为空")

        logger.info("主账号凭证读取成功")
        return master_ak_id, master_ak_secret

    @staticmethod
    def get_role_config(role_section: str) -> tuple[str, str]:
        """获取指定角色的ARN和原始SessionName"""
        config_path = AliyunMultiRoleRenewalQuery._get_config_path()
        config = configparser.ConfigParser()
        config.read(config_path, encoding='utf-8')

        try:
            role_arn = config.get(role_section, 'role_arn')
            role_session_name = config.get(role_section, 'role_session_name')
        except (configparser.NoSectionError, configparser.NoOptionError) as e:
            logger.error(f"角色{role_section}配置错误：{str(e)}")
            raise ValueError(f"角色{role_section}配置错误：{str(e)}")

        if not all([role_arn, role_session_name]):
            logger.error(f"角色{role_section}的role_arn和role_session_name不能为空")
            raise ValueError(f"角色{role_section}的role_arn和role_session_name不能为空")

        return role_arn, role_session_name

    @staticmethod
    def create_client_by_role(role_section: str) -> BssOpenApi20171214Client:
        """根据指定角色分组创建BSS OpenAPI客户端"""
        logger.info(f"\n开始初始化角色{role_section}的客户端...")

        # 1. 获取主账号凭证
        master_ak_id, master_ak_secret = AliyunMultiRoleRenewalQuery.get_master_credentials()

        # 2. 获取角色配置
        role_arn, raw_session_name = AliyunMultiRoleRenewalQuery.get_role_config(role_section)

        # 3. 清洗SessionName（符合API规范）
        api_safe_session_name = AliyunMultiRoleRenewalQuery.sanitize_session_name(raw_session_name)
        logger.info(f"角色{role_section}：原始SessionName={raw_session_name}，清洗后={api_safe_session_name}")

        # 4. 初始化STS客户端
        sts_config = open_api_models.Config(
            access_key_id=master_ak_id,
            access_key_secret=master_ak_secret,
            endpoint='sts.aliyuncs.com'
        )
        sts_client = StsClient(sts_config)

        # 5. 申请临时访问凭证
        assume_role_request = AssumeRoleRequest(
            role_arn=role_arn,
            role_session_name=api_safe_session_name,
            duration_seconds=3600  # 有效期1小时
        )
        logger.info(f"角色{role_section}：发起临时凭证申请...")
        response = sts_client.assume_role(assume_role_request)

        # 6. 验证凭证并创建BSS客户端
        if not response.body.credentials:
            logger.error(f"角色{role_section}获取临时凭证失败")
            raise Exception(f"角色{role_section}获取临时凭证失败")

        credentials = response.body.credentials
        bss_config = open_api_models.Config(
            access_key_id=credentials.access_key_id,
            access_key_secret=credentials.access_key_secret,
            security_token=credentials.security_token,
            endpoint='business.ap-southeast-1.aliyuncs.com'
        )

        logger.info(f"✅ 角色{role_section}：BSS客户端创建成功")
        return BssOpenApi20171214Client(bss_config)

    @staticmethod
    def get_last_month() -> str:
        """获取上一个月的字符串（格式：YYYY-MM）"""
        today = datetime.today()
        first_day_of_current = today.replace(day=1)
        last_day_of_last = first_day_of_current - timedelta(days=1)
        last_month = last_day_of_last.strftime("%Y-%m")
        logger.info(f"自动获取上一个月周期：{last_month}")
        return last_month

    @staticmethod
    def process_single_role_instances(
            response_body: Dict[str, Any],
            role_section: str,
            role_session_name: str
    ) -> List[Dict[str, Any]]:
        """处理单个角色的实例数据，返回结构化列表"""
        logger.info(f"角色{role_section}：开始处理实例数据...")

        if not response_body.get('Success') or not response_body.get('Data'):
            logger.warning(f"角色{role_section}：无有效实例数据返回")
            return []

        data = response_body['Data']
        instances = data.get('InstanceList', [])
        # 处理非列表类型的返回结果
        instances = instances if isinstance(instances, list) else [instances] if instances else []

        logger.info(f"角色{role_section}：原始实例数据条数：{len(instances)}")

        # 结构化处理数据（新增状态、产品类型、产品代码、地域字段）
        processed_data = []
        for instance in instances:
            processed_data.append({
                '资源id': instance.get('InstanceID', ''),
                '资源所属账号': role_session_name,  # 保留原始名称
                '资源到期时间': instance.get('EndTime', ''),
                '状态': instance.get('Status', ''),  # 新增：实例状态
                '产品类型': instance.get('ProductType', ''),  # 新增：产品类型
                '产品代码': instance.get('ProductCode', ''),  # 新增：产品代码
                '地域': instance.get('Region', '')  # 新增：实例所属地域
            })

        logger.info(f"角色{role_section}：处理完成{len(processed_data)}条实例数据")
        return processed_data

    @staticmethod
    def init_output_file():
        """初始化输出CSV文件（覆盖模式）- 新增列配置"""
        global OUTPUT_FILE_PATH
        # 确保数据目录存在
        os.makedirs(DATA_DIR, exist_ok=True)

        # 定义输出字段（新增状态、产品类型、产品代码、地域）
        output_fields = ['资源id', '资源所属账号', '资源到期时间', '状态', '产品类型', '产品代码', '地域']

        # 写入表头
        with open(OUTPUT_FILE_PATH, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=output_fields)
            writer.writeheader()

        logger.info(f"✅ 输出文件初始化完成：{OUTPUT_FILE_PATH}")

    @staticmethod
    def append_to_output_file(detail_list: List[Dict[str, Any]]):
        """将实例数据追加到输出CSV文件"""
        if not detail_list:
            logger.warning("无有效数据可写入文件，跳过追加操作")
            return

        output_fields = ['资源id', '资源所属账号', '资源到期时间', '状态', '产品类型', '产品代码', '地域']
        with open(OUTPUT_FILE_PATH, 'a', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=output_fields)
            writer.writerows(detail_list)

        logger.info(f"✅ 成功写入{len(detail_list)}条数据到文件")

    @staticmethod
    def query_single_role(role_section: str) -> List[Dict[str, Any]]:
        """查询单个角色的续费实例数据"""
        logger.info(f"\n" + "-" * 50)
        logger.info(f"开始查询角色：{role_section}")
        logger.info("-" * 50)

        try:
            # 1. 获取原始角色SessionName（用于CSV显示）
            original_session_name = AliyunMultiRoleRenewalQuery.get_role_config(role_section)[1]

            # 2. 创建BSS客户端
            client = AliyunMultiRoleRenewalQuery.create_client_by_role(role_section)

            # 3. 构造查询请求
            query_request = bss_open_api_20171214_models.QueryAvailableInstancesRequest(
                subscription_type='Subscription',
                renew_status='ManualRenewal',
                page_size=100
            )

            # 4. 发起查询
            logger.info(f"角色{role_section}：发起续费实例查询请求...")
            response = client.query_available_instances_with_options(
                query_request,
                util_models.RuntimeOptions()
            )

            # 5. 处理响应数据
            response_body = response.body.to_map() if hasattr(response.body, 'to_map') else dict(response.body)
            return AliyunMultiRoleRenewalQuery.process_single_role_instances(
                response_body,
                role_section,
                original_session_name
            )

        except Exception as e:
            err_msg = str(e)
            logger.error(f"❌ 角色{role_section}查询失败：{err_msg}", exc_info=True)
            return []

    @staticmethod
    def main(args: List[str]) -> None:
        """主入口函数"""
        # 程序启动日志
        logger.info("=" * 80)
        logger.info(f"程序启动：阿里云多角色续费实例查询工具（时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）")
        logger.info("=" * 80)

        # 解析命令行参数
        parser = argparse.ArgumentParser(description='阿里云多角色续费实例查询工具')
        parser.add_argument('-r', '--role', type=str, help='指定单个角色分组名（如 aliyun-skyarch）')
        parsed_args = parser.parse_args(args)
        logger.info(f"命令行参数：role={parsed_args.role or '未指定'}")

        try:
            # 1. 确定查询的角色列表
            if parsed_args.role:
                role_sections = [parsed_args.role]
                logger.info(f"指定查询单个角色：{parsed_args.role}")
            else:
                role_sections = AliyunMultiRoleRenewalQuery.get_all_role_sections()
                if not role_sections:
                    logger.info("未找到任何角色分组，程序退出")
                    return
                logger.info(f"未指定角色，将查询所有{len(role_sections)}个角色")

            # 2. 确定输出文件路径
            last_month = AliyunMultiRoleRenewalQuery.get_last_month()
            global OUTPUT_FILE_PATH
            OUTPUT_FILE_PATH = os.path.join(DATA_DIR, f'aliyun_renewal_bill_{last_month}.csv')

            # 3. 初始化输出文件
            AliyunMultiRoleRenewalQuery.init_output_file()

            # 4. 批量查询所有角色
            total_count = 0
            for role in role_sections:
                instances = AliyunMultiRoleRenewalQuery.query_single_role(role)
                if instances:
                    AliyunMultiRoleRenewalQuery.append_to_output_file(instances)
                    total_count += len(instances)
                    logger.info(f"角色{role}：{len(instances)}条数据已写入文件")
                else:
                    logger.info(f"角色{role}：无有效数据可写入文件")

            # 5. 输出最终统计（日志+控制台）
            logger.info(f"\n" + "=" * 80)
            logger.info(f"查询完成！")
            logger.info(f"查询角色数：{len(role_sections)}")
            logger.info(f"总有效实例数：{total_count}")
            logger.info(f"输出文件：{OUTPUT_FILE_PATH}")
            logger.info("=" * 80 + "\n")

            # 控制台友好输出
            print(f"\n" + "=" * 80)
            print(f"查询完成！")
            print(f"=" * 80)
            print(f"查询角色数：{len(role_sections)}")
            print(f"总有效实例数：{total_count}")
            print(f"输出文件：{OUTPUT_FILE_PATH}")
            print(f"=" * 80)

        except Exception as e:
            logger.error(f"❌ 程序执行失败：{str(e)}", exc_info=True)
            print(f"\n程序执行失败：{str(e)}")
            sys.exit(1)


# ===================== 程序入口 =====================
if __name__ == '__main__':
    AliyunMultiRoleRenewalQuery.main(sys.argv[1:])
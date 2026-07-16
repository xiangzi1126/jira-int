import requests
import csv
import os
import logging
from datetime import datetime
import json


def init_logger():
    """初始化日志，追加输出到 ../../jira/log/renewal_instances.log 和控制台"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    log_dir = os.path.normpath(os.path.join(current_dir, '../../jira/log'))
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, 'renewal_instances.log')

    logging.basicConfig(
        level=logging.DEBUG,  # DEBUG级别，查看详细数据
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file, mode='a', encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    return logging.getLogger(__name__)


logger = init_logger()


def send_renewal_request():
    logger.info("=" * 50)
    logger.info("开始执行云资源续费接口调用")

    # 1. 定义文件路径和接口地址
    current_dir = os.path.dirname(os.path.abspath(__file__))
    csv_path = os.path.normpath(os.path.join(current_dir, '../../jira/data/jira_get_need_renewal.csv'))
    api_url = "https://securaifc-renew-gnzmqvzagp.cn-shanghai.fcapp.run/renew/instances"

    # 2. 校验CSV文件是否存在
    if not os.path.exists(csv_path):
        err_msg = f"续费数据CSV文件不存在 - {csv_path}"
        logger.error(err_msg)
        print(err_msg)
        return

    # 3. 读取并解析CSV数据（先保存所有实例数据，用于后续更新状态）
    renew_instances = []
    csv_all_data = []  # 保存CSV所有行数据（含原始字段）
    instance_id_map = {}  # 用InstanceID做key，映射行数据索引，方便后续更新
    try:
        with open(csv_path, 'r', encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f)

            # 校验必要的列是否存在
            required_cols = ['平台', '云账号UID', '账号', '资源ID', '续费时长（月）', '产品代码', '产品类型', '地域']
            for col in required_cols:
                if col not in reader.fieldnames:
                    err_msg = f"CSV文件缺少必要列：{col}"
                    logger.error(err_msg)
                    print(err_msg)
                    return

            # 读取所有行数据，并初始化续费状态和error_msg列
            for idx, row in enumerate(reader):
                # 初始化新增列
                row['续费状态'] = ''
                row['error_msg'] = ''

                csv_all_data.append(row)
                instance_id = row['资源ID'].strip()
                if instance_id:
                    instance_id_map[instance_id] = idx  # 记录每个实例ID对应的行索引

                try:
                    # 数据类型转换：accountId 和 Period 必须为整数
                    account_id = int(row['云账号UID'].strip()) if row['云账号UID'].strip() else None
                    period = int(row['续费时长（月）']) if row['续费时长（月）'] else None

                    if not account_id or not period:
                        logger.warning(f"跳过第 {idx + 1} 行数据（账号UID或续费时长为空）：{row}")
                        continue

                    # 构造实例数据
                    instance = {
                        "site": row['平台'],
                        "accountId": account_id,
                        "Alias": row['账号'],
                        "InstanceID": instance_id,
                        "Period": period,
                        "ProductCode": row['产品代码'],
                        "ProductType": row['产品类型'],
                        "Region": row['地域']
                    }
                    renew_instances.append(instance)
                    logger.debug(f"解析第 {idx + 1} 行数据成功：{instance}")

                except ValueError as e:
                    logger.warning(f"跳过第 {idx + 1} 行数据（格式错误）：{row}, 错误: {e}")
                    continue

        if not renew_instances:
            err_msg = "CSV文件中未找到有效的续费实例数据"
            logger.error(err_msg)
            print(err_msg)
            return

        logger.info(f"成功读取 {len(renew_instances)} 条续费实例数据")

    except Exception as e:
        err_msg = f"读取CSV文件失败：{str(e)}"
        logger.error(err_msg, exc_info=True)
        print(err_msg)
        return

    # 4. 构造 Jira Context 及完整 Payload
    today_str = datetime.now().strftime('%Y-%m-%d')
    now = datetime.now()
    trigger_time = now.strftime('%Y-%m-%dT%H:%M:%S+08:00')

    # 修复字段名：renew_instances 改为 wait_renew_instances
    payload = {
        "jira_context": {
            "parent_ticket_id": f"jira_{today_str}",
            "parent_ticket_summary": f"jira_{today_str}_云资源自动续费",
            "trigger_time": trigger_time
        },
        "wait_renew_instances": renew_instances
    }

    logger.info(f"构造请求Payload完成，parent_ticket_id: {payload['jira_context']['parent_ticket_id']}")
    logger.debug(f"完整请求 Payload: {json.dumps(payload, ensure_ascii=False, indent=2)}")

    # 5. 发送 POST 请求
    response_json = None
    try:
        logger.info(f"正在向接口发送请求：{api_url}")
        headers = {'Content-Type': 'application/json'}
        response = requests.post(api_url, json=payload, headers=headers, timeout=600)

        # 记录响应状态
        logger.info(f"接口响应状态码：{response.status_code}")

        # 尝试解析响应JSON
        try:
            response_json = response.json()
            logger.info(f"接口响应内容：{json.dumps(response_json, ensure_ascii=False, indent=2)}")
        except Exception as e:
            logger.error(f"解析响应JSON失败：{e}，响应文本：{response.text}")
            response_json = None

        # 判断请求是否成功 (通常 2xx 为成功)
        response.raise_for_status()

        logger.info("续费接口调用成功（请求发送成功，具体实例续费结果见详情）")
        print("续费请求发送成功！")

    except requests.exceptions.RequestException as e:
        err_msg = f"发送HTTP请求失败：{str(e)}"
        logger.error(err_msg, exc_info=True)
        print(err_msg)
    except Exception as e:
        err_msg = f"接口调用过程中发生未知错误：{str(e)}"
        logger.error(err_msg, exc_info=True)
        print(err_msg)

    # 6. 解析接口响应，更新CSV中的续费状态和错误信息
    if response_json and 'details' in response_json and isinstance(response_json['details'], list):
        logger.info(f"开始更新CSV文件的续费状态，共{len(response_json['details'])}个实例结果")

        # 遍历接口返回的每个实例详情
        for detail in response_json['details']:
            instance_id = detail.get('instance_id', '').strip()
            if not instance_id or instance_id not in instance_id_map:
                logger.warning(f"未找到实例ID[{instance_id}]对应的CSV行数据，跳过更新")
                continue

            # 获取该行数据的索引
            row_idx = instance_id_map[instance_id]
            # 更新续费状态（布尔值转字符串，方便CSV存储）
            csv_all_data[row_idx]['续费状态'] = str(detail.get('success', False)).lower()
            # 更新错误信息
            csv_all_data[row_idx]['error_msg'] = detail.get('error_msg', '')

            logger.debug(
                f"更新实例[{instance_id}]状态：success={csv_all_data[row_idx]['续费状态']}, error_msg={csv_all_data[row_idx]['error_msg']}")

        # 7. 覆盖写入更新后的CSV文件
        try:
            # 获取原始表头 + 新增列
            original_headers = list(csv_all_data[0].keys()) if csv_all_data else []
            # 确保新增列在表头末尾
            if '续费状态' not in original_headers:
                original_headers.append('续费状态')
            if 'error_msg' not in original_headers:
                original_headers.append('error_msg')

            with open(csv_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=original_headers)
                writer.writeheader()
                writer.writerows(csv_all_data)

            logger.info(f"CSV文件已更新续费状态，文件路径：{csv_path}")
            print(f"CSV文件更新完成！已新增/更新【续费状态】和【error_msg】列")
        except Exception as e:
            err_msg = f"写入更新后的CSV文件失败：{str(e)}"
            logger.error(err_msg, exc_info=True)
            print(err_msg)
    else:
        logger.warning("接口响应中无有效的实例详情数据，未更新CSV")

    # 8. 任务结束
    logger.info("云资源续费接口调用流程结束")
    logger.info("=" * 50 + "\n")


if __name__ == "__main__":
    send_renewal_request()
#!/bin/bash

# --- 脚本初始化与配置 ---
# 确保在任何命令失败时立即退出，以便排查问题。
set -e

# 更新软件包列表并安装必要的工具。
# -y 选项会自动确认所有安装提示。
sudo apt-get update
sudo apt-get install -y curl parallel jq

# --- 变量定义 ---
# 日志名称，用于在Cloud Logging中筛选。
readonly LOG_NAME="url_request_monitor_log"
# 从GCS下载的URL列表的本地路径。
readonly URL_LIST_FILE="/tmp/urls.txt"
# 用于存放成功和失败URL记录的临时目录。
readonly RESULTS_DIR="/tmp/results"
# 成功URL的本地记录文件。
readonly SUCCESS_LOG="/tmp/results/success.log"
# 失败URL的本地记录文件。
readonly FAILURE_LOG="/tmp/results/failure.log"

# 创建临时结果目录。
sudo rm -rf /tmp/results
mkdir -p "$RESULTS_DIR"

# --- 日志记录辅助函数 ---
# 一个简单的函数，用于发送结构化日志到Cloud Logging，增加代码可读性。
log_to_gcp() {{
    local message="$1"
    local severity="$2"
    local payload_extra="$3"
    local final_payload

    # 步骤1：使用jq安全地创建基础JSON
    final_payload=$(jq -n \
                      --arg msg "$message" \
                      --arg sev "$severity" \
                      '{{message: $msg, severity: $sev}}')

    # 步骤2：如果存在额外的payload，安全地进行合并
    if [[ -n "$payload_extra" ]]; then
        # 使用printf确保每个JSON对象占独立一行，再传给jq进行合并
        final_payload=$(printf "%s\n%s\n" "$final_payload" "$payload_extra" | jq -s '.[0] * .[1]')
    fi

    # 步骤3：将$final_payload用双引号包围，作为直接参数传递，不再使用标准输入
    gcloud logging write "$LOG_NAME" --payload-type=json "$final_payload"
}}

# --- 主要逻辑 ---
# 1. 从GCS下载URL列表
#    注意：请在Pulumi代码中将gcs_url_list_path替换为实际的GCS路径。
log_to_gcp "Starting URL processing script." "INFO" '{{"vm_name": "'$(hostname)'", "region": "{region}", "task_id":"{shard_suffix}"}}'
gsutil cp {gcs_url_list_path} "$URL_LIST_FILE"
if [ $? -ne 0 ]; then
    log_to_gcp "FATAL: Failed to download URL list from GCS. Exiting." "CRITICAL" '{{"vm_name": "'$(hostname)'", "region": "{region}", "task_id":"{shard_suffix}"}}'
    exit 1
fi

echo "--- DEBUG 1: Final JSON Payload ---"

readonly TOTAL_URLS=$(wc -l < "$URL_LIST_FILE" | tr -d ' ')
echo "TOTAL_URLS = $TOTAL_URLS"
log_to_gcp "URL list downloaded successfully." "INFO" '{{"vm_name": "'$(hostname)'", "region": "{region}", "task_id":"{shard_suffix}", "total_urls": '$TOTAL_URLS'}}'

echo "--- DEBUG 1.5: Final JSON Payload ---"

# 2. 定义处理单个URL的函数
#    这个函数会被'parallel'命令并行调用。
process_url() {{
    local url="$1"
    # 设置60秒超时，-L跟随重定向，-s静默模式，-o将下载内容丢弃，-w获取最终的HTTP状态码。
    http_code=$(curl -L -s -o /dev/null -w "%{{http_code}}" --max-time 60 "$url")
    
    # 检查HTTP状态码是否为2xx或3xx（通常表示成功或重定向成功）。
    if [[ "$http_code" =~ ^[23] ]]; then
        echo "$url" >> "$SUCCESS_LOG"
    else
        # 将失败的URL同时记录到本地文件和Cloud Logging。
        echo "$url" >> "$FAILURE_LOG"
        
        local extra_payload
        extra_payload=$(jq -n \
          --arg vm_name "$(hostname)" \
          --arg region "{region}" \
          --arg task_id "{shard_suffix}" \
          --arg url "$url" \
          --arg http_code "$http_code" \
          '{{
            "vm_name": $vm_name,
            "region": $region,
            "task_id": $task_id,
            "url": $url,
            "http_code": ($http_code | tonumber)
          }}')
        
        log_to_gcp "Request failed for URL." "WARNING" "$extra_payload"
    fi
}}
# 将函数导出，以便'parallel'可以调用它。
export LOG_NAME
export SUCCESS_LOG
export FAILURE_LOG

export -f process_url
export -f log_to_gcp

echo "--- DEBUG 2: Final JSON Payload ---"

# 3. 使用GNU Parallel并行执行所有任务
#    -j 100: 最多同时运行100个任务，可根据机器性能和网络调整。
#    --eta: 显示预计完成时间。
log_to_gcp "Starting parallel processing of URLs..." "INFO" '{{"vm_name": "'$(hostname)'", "region": "{region}", "task_id":"{shard_suffix}", "concurrent_jobs": 200}}'
cat "$URL_LIST_FILE" | parallel -j 200 process_url

echo "--- DEBUG 3: Final JSON Payload ---"

# 4. 生成并发送最终的摘要报告
log_to_gcp "All URL processing finished. Generating final summary." "NOTICE" '{{"vm_name": "'$(hostname)'", "region": "{region}", "task_id":"{shard_suffix}"}}'

# 安全地统计行数，即使文件不存在也不会报错。
SUCCESS_COUNT=$(cat "$SUCCESS_LOG" 2>/dev/null | wc -l || echo 0)
FAILURE_COUNT=$(cat "$FAILURE_LOG" 2>/dev/null | wc -l || echo 0)

# 使用awk进行浮点数计算，避免shell的整数除法问题。
COMPLETION_RATE=$(awk -v total="$TOTAL_URLS" -v success="$SUCCESS_COUNT" -v failure="$FAILURE_COUNT" \
  'BEGIN {{
    if (total > 0) {{
        printf "%%.2f", ((success + failure) / total) * 100
    }} else {{
        print 0
    }}
  }}')

SUCCESS_RATE=$(awk -v total="$TOTAL_URLS" -v success="$SUCCESS_COUNT" \
  'BEGIN {{
    if (total > 0) {{
        printf "%%.2f", (success / total) * 100
    }} else {{
        print 0
    }}
  }}')

echo "SUCCESS_COUNT = $SUCCESS_COUNT"
echo "FAILURE_COUNT = $FAILURE_COUNT"
echo "COMPLETION_RATE = $COMPLETION_RATE"
echo "SUCCESS_RATE = $SUCCESS_RATE"

# 将失败的URL列表（最多前100个）格式化为JSON数组。
# 默认为空数组。
FAILED_URLS_SAMPLE='[]' 
# 检查失败日志文件是否存在且不为空。
if [ -s "$FAILURE_LOG" ]; then 
    # -R: 读取原始字符串; -s: 将所有输入合并成一个数组。
    # 'split("\n") | map(select(length > 0))': 按换行符分割并移除空行。
    FAILED_URLS_SAMPLE=$(head -n 100 "$FAILURE_LOG" | jq -R -s 'split("\n") | map(select(length > 0))')
fi

echo "FAILED_URLS_SAMPLE = $FAILED_URLS_SAMPLE"

# 构建最终的摘要JSON。
SUMMARY_PAYLOAD_BASE=$(jq -n \
  --arg vm_name "$(hostname)" \
  --arg region {region} \
  --arg task_id {shard_suffix} \
  --arg total "$TOTAL_URLS" \
  --arg success "$SUCCESS_COUNT" \
  --arg failure "$FAILURE_COUNT" \
  --arg comp_rate "$COMPLETION_RATE" \
  --arg success_rate "$SUCCESS_RATE" \
  '{{
    "message": "Task finished. Final summary below.",
    "severity": "NOTICE",
    "vm_name": $vm_name,
    "region": $region,
    "task_id": $task_id,
    "total_urls": ($total | tonumber),
    "success_count": ($success | tonumber),
    "failure_count": ($failure | tonumber),
    "completion_rate_percent": ($comp_rate | tonumber),
    "success_rate_percent": ($success_rate | tonumber)
  }}')

# 步骤 B: 将第一部分与失败URL样本（已经是JSON格式）安全地合并成最终的payload。
SUMMARY_PAYLOAD=$(jq -n \
  --argjson base "$SUMMARY_PAYLOAD_BASE" \
  --argjson sample "$FAILED_URLS_SAMPLE" \
  '$base + {{"failed_urls_sample": $sample}}')  

echo "SUMMARY_PAYLOAD = $SUMMARY_PAYLOAD"

echo "--- DEBUG 4: Final JSON Payload ---"

log_to_gcp "Task finished. Final summary below." "NOTICE" "$SUMMARY_PAYLOAD"

# --- 清理工作 ---
# 删除临时文件。
rm -rf "$RESULTS_DIR" "$URL_LIST_FILE"
log_to_gcp "Cleanup complete. Script finished." "INFO" '{{"vm_name": "'$(hostname)'", "region": "{region}", "task_id":"{shard_suffix}"}}'

# --- (可选) 任务完成后自动销毁虚拟机 ---
# 如果需要，可以取消下面这行的注释。请确保服务账号有删除实例的权限。
# gcloud compute instances delete "$(hostname)" --zone="{{zone}}" --quiet
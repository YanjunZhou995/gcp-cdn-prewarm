import pulumi
import pulumi_gcp as gcp
from pulumi.dynamic import Resource, ResourceProvider, CreateResult
import math
import os

# --------------------------------------------------------------------------
# 步骤一：定义动态资源提供者 (Dynamic Resource Provider)
# 这是执行 "下载-分割-上传" 这种命令式操作的核心
# --------------------------------------------------------------------------
class UrlSharderProvider(ResourceProvider):
    def create(self, props):
        # 从Pulumi传递过来的属性中获取所有需要的参数
        num_shards = int(props["num_shards"])
        bucket_name = props["bucket_name"]
        source_blob_name = props["source_blob_name"]
        destination_prefix = props["destination_prefix"]

        # 动态导入 GCS 库
        try:
            from google.cloud import storage
        except ImportError:
            raise Exception("google-cloud-storage library not found. Please run 'pip install google-cloud-storage'")

        print(f"Starting URL sharding process for {source_blob_name} into {num_shards} shards.")

        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)

        # 1. 下载源URL列表文件
        print(f"Downloading {source_blob_name} from GCS bucket {bucket_name}...")
        source_blob = bucket.blob(source_blob_name)
        local_source_path = "/tmp/all_urls.txt"
        source_blob.download_to_filename(local_source_path)
        print("Download complete.")

        # 2. 读取文件并计算如何分割
        with open(local_source_path, 'r') as f:
            lines = f.readlines()
        
        total_lines = len(lines)
        lines_per_shard = math.ceil(total_lines / num_shards)
        print(f"Total URLs: {total_lines}, URLs per shard: {lines_per_shard}")

        # 3. 分割文件并上传
        created_shard_paths = []
        for i in range(num_shards):
            shard_lines = lines[i * lines_per_shard:(i + 1) * lines_per_shard]
            if not shard_lines:
                continue # 如果分片为空则跳过

            shard_file_name = f"url_shard_{i:02d}.txt" # e.g., url_shard_00.txt
            local_shard_path = f"/tmp/{shard_file_name}"

            with open(local_shard_path, 'w') as f:
                f.writelines(shard_lines)

            # 定义在GCS中的目标路径
            destination_blob_name = f"{destination_prefix}/{shard_file_name}"
            shard_blob = bucket.blob(destination_blob_name)
            
            print(f"Uploading shard {i+1}/{num_shards} to {destination_blob_name}...")
            shard_blob.upload_from_filename(local_shard_path)
            
            # 记录创建好的GCS路径
            created_shard_paths.append(f"gs://{bucket_name}/{destination_blob_name}")
            
            # 清理本地临时分片文件
            os.remove(local_shard_path)
        
        print("All shards uploaded successfully.")
        
        # 清理本地源文件
        os.remove(local_source_path)

        # create() 函数必须返回一个 CreateResult 对象
        # 'outs' 字典中的内容将成为这个动态资源的输出属性
        return CreateResult(
            id_="url-sharder-resource", # 任意唯一的ID
            outs={"shard_paths": created_shard_paths}
        )

# --------------------------------------------------------------------------
# 步骤二：定义动态资源本身
# 这是上面Provider的一个封装，使其能在Pulumi程序中像普通资源一样使用
# --------------------------------------------------------------------------
class UrlSharder(Resource):
    def __init__(self, name, num_shards, bucket_name, source_blob_name, destination_prefix, opts=None):
        super().__init__(UrlSharderProvider(), name, {
            "num_shards": num_shards,
            "bucket_name": bucket_name,
            "source_blob_name": source_blob_name,
            "destination_prefix": destination_prefix,
            "shard_paths": None, # 定义输出属性
        }, opts)

# --------------------------------------------------------------------------
# 步骤三：主逻辑 - 读取配置并创建资源
# --------------------------------------------------------------------------
# 1. 读取Pulumi配置
config = pulumi.Config()
type = config.require('type')
num_vms = config.require('num_vms')
total_attempts = config.require("total_attempts")

bucket_name = config.require("gcs_bucket_name")
source_url_path = config.require("gcs_url_list_path")
shard_output_dir = config.require("gcs_shard_output_dir")


# 在这里设置自定义超时
long_timeout = pulumi.CustomTimeouts(create="20m")

# 2. 实例化并运行我们的动态资源来分割文件
# Pulumi会先完成这个资源的创建，然后再继续创建依赖它的其他资源
url_shards = UrlSharder("url-file-sharder",
    num_shards=num_vms,
    bucket_name=bucket_name,
    source_blob_name=source_url_path,
    destination_prefix=shard_output_dir
)

# --- 集群配置 ---
# 机器规格建议使用高CPU类型，因为任务是网络和CPU密集型

# 注意：脚本内容保持不变，我们只在创建VM时动态传入不同的GCS路径
startup_script_template = '''
#!/bin/bash

# --- 脚本初始化与配置 ---
# 确保在任何命令失败时立即退出，以便排查问题。
set -e
set -x # ★★★ 新增：开启执行过程跟踪 ★★★

# 更新软件包列表并安装必要的工具。
# -y 选项会自动确认所有安装提示。
# 增加一个循环，最多重试3次来执行 apt-get update
for i in {{1..3}}; do
    echo "--- [Attempt $i/3] Running apt-get update... ---"
    if sudo apt-get update; then
        echo "--- apt-get update successful! ---"
        break # 如果成功，就跳出循环
    fi

    if [ $i -lt 3 ]; then
        echo "--- apt-get update failed. Retrying in 15 seconds... ---"
        sleep 15 # 等待15秒再重试
    else
        echo "--- ERROR: apt-get update failed after 3 attempts. ---"
        exit 1 # 3次都失败了，则让脚本以失败状态退出
    fi
done
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

# ★★★ 新增：从Pulumi接收动态的请求次数 ★★★
readonly TOTAL_ATTEMPTS={total_attempts}

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

echo "--- DEBUG 1: Start downloading URL List ---"

# --- 主要逻辑 ---
# 1. 从GCS下载URL列表
#    注意：请在Pulumi代码中将gcs_url_list_path替换为实际的GCS路径。
log_to_gcp "Starting URL processing script." "INFO" '{{"vm_name": "'$(hostname)'", "region": "{region}", "task_id":"{shard_suffix}", "gcs_url_shard_path":"{gcs_url_list_path}"}}'
gsutil cp {gcs_url_list_path} "$URL_LIST_FILE"
if [ $? -ne 0 ]; then
    log_to_gcp "FATAL: Failed to download URL list from GCS. Exiting." "CRITICAL" '{{"vm_name": "'$(hostname)'", "region": "{region}", "task_id":"{shard_suffix}"}}'
    exit 1
fi

readonly TOTAL_URLS=$(grep -c . "$URL_LIST_FILE")
readonly TOTAL_REQUESTS=$((TOTAL_URLS * TOTAL_ATTEMPTS)) # 新增：定义总请求数
echo "TOTAL_URLS = $TOTAL_URLS"
echo "TOTAL_REQUESTS = $TOTAL_REQUESTS" # 新增：打印总请求数
log_to_gcp "URL list downloaded successfully." "INFO" '{{"vm_name": "'$(hostname)'", "region": "{region}", "task_id":"{shard_suffix}", "gcs_url_shard_path":"{gcs_url_list_path}", "total_urls": '$TOTAL_URLS', "total_requests": '$TOTAL_REQUESTS'}}'

# 2. 定义处理单个URL的函数
#    这个函数会被'parallel'命令并行调用。
process_url() {{
    local url="$1"

    # ★★★ 新增：定义一个独立的调试日志文件 ★★★
    local DEBUG_LOG="/tmp/results/process_url_debug.log"

    # ★★★ 新增：打印当前任务的完整环境变量 ★★★
    echo "--- Environment for URL: $url ---" >> "$DEBUG_LOG"
    env >> "$DEBUG_LOG"
    echo "-------------------------------------" >> "$DEBUG_LOG"

    echo "[DEBUG] Starting process_url for: $url" >> "$DEBUG_LOG"

    for (( i=1; i<={total_attempts}; i++ )); do
        # 设置60秒超时，-L跟随重定向，-s静默模式，-o将下载内容丢弃，-w获取最终的HTTP状态码。
        http_code=$(curl -L -s -o /dev/null -w "%{{http_code}}" --max-time 60 "$url")

        # ★★★ 新增：无论成功失败，都记录http_code的值 ★★★
        echo "[DEBUG] curl finished for: $url with http_code: $http_code" >> "$DEBUG_LOG"

        # 检查HTTP状态码是否为2xx或3xx（通常表示成功或重定向成功）。
        if [[ "$http_code" =~ ^[23] ]]; then
            echo "$url" >> "$SUCCESS_LOG"
        else
            # 将失败的URL同时记录到本地文件和Cloud Logging。
            echo "$url" >> "$FAILURE_LOG"
        
            # 为当次失败发送详细日志到Cloud Logging
            local extra_payload
            extra_payload=$(jq -n \
              --arg vm_name "$(hostname)" \
              --arg region {region} \
              --arg task_id {shard_suffix} \
              --arg url "$url" \
              --arg http_code "$http_code" \
              --arg attempt_num "$i" \
              '{{
                "vm_name": $vm_name,
                "region": $region,
                "task_id": $task_id,
                "failed_url": $url,
                "http_code": ($http_code | tonumber),
                "attempt_num": ($attempt_num | tonumber)
              }}')

            log_to_gcp "Request failed for URL." "WARNING" "$extra_payload"
        fi

        # 在两次请求之间短暂休息1秒，避免对服务器造成过大压力
        if [[ $i -lt {total_attempts} ]]; then
            sleep 1
        fi
    done
}}
# 将函数导出，以便'parallel'可以调用它。
export LOG_NAME
export SUCCESS_LOG
export FAILURE_LOG

export -f process_url
export -f log_to_gcp

echo "--- DEBUG 2: Start sending requests ---"

# 3. 使用GNU Parallel并行执行所有任务
#    -j 100: 最多同时运行100个任务，可根据机器性能和网络调整。
#    --eta: 显示预计完成时间。
log_to_gcp "Starting parallel processing of URLs..." "INFO" '{{"vm_name": "'$(hostname)'", "region": "{region}", "task_id":"{shard_suffix}", "concurrent_jobs": 200}}'
cat "$URL_LIST_FILE" | parallel -j 200 process_url

echo "--- DEBUG 3: Start generating final summary ---"

# 4. 生成并发送最终的摘要报告
log_to_gcp "All URL processing finished. Generating final summary." "NOTICE" '{{"vm_name": "'$(hostname)'", "region": "{region}", "task_id":"{shard_suffix}"}}'

# 安全地统计行数，即使文件不存在也不会报错。
SUCCESS_COUNT=$(cat "$SUCCESS_LOG" 2>/dev/null | wc -l || echo 0)
FAILURE_COUNT=$(cat "$FAILURE_LOG" 2>/dev/null | wc -l || echo 0)

# 使用awk进行浮点数计算，避免shell的整数除法问题。
COMPLETION_RATE=$(awk -v total="$TOTAL_REQUESTS" -v success="$SUCCESS_COUNT" -v failure="$FAILURE_COUNT" \
  'BEGIN {{
    if (total > 0) {{
        printf "%.2f", ((success + failure) / total) * 100
    }} else {{
        print 0
    }}
  }}')

SUCCESS_RATE=$(awk -v total="$TOTAL_REQUESTS" -v success="$SUCCESS_COUNT" \
  'BEGIN {{
    if (total > 0) {{
        printf "%.2f", (success / total) * 100
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
    # 步骤1: 安全地读取最多100个非空行到 Bash 数组 'urls' 中。
    # 这种方法比长管道更稳定。
    urls=()
    while IFS= read -r line && [ ${{#urls[@]}} -lt 100 ]; do
        # 如果行不为空，则添加到数组中
        if [ -n "$line" ]; then
            urls+=("$line")
        fi
    done < "$FAILURE_LOG"

    # 步骤2: 如果数组中有内容，则使用 jq 将其转换为 JSON 格式。
    # 这个转换过程是标准且可靠的。
    if [ ${{#urls[@]}} -gt 0 ]; then
        FAILED_URLS_SAMPLE=$(printf '%s\n' "${{urls[@]}}" | jq -R . | jq -s .)
    fi
fi

# echo "FAILED_URLS_SAMPLE = $FAILED_URLS_SAMPLE"

# 构建最终的摘要JSON。
SUMMARY_PAYLOAD_BASE=$(jq -n \
  --arg vm_name "$(hostname)" \
  --arg region {region} \
  --arg task_id {shard_suffix} \
  --arg total "$TOTAL_URLS" \
  --arg total_attempts {total_attempts} \
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
    "attempts_per_url": $total_attempts,
    "success_count": ($success | tonumber),
    "failure_count": ($failure | tonumber),
    "completion_rate_percent": ($comp_rate | tonumber),
    "success_rate_percent": ($success_rate | tonumber)
  }}')

# 步骤 B: 将第一部分与失败URL样本（已经是JSON格式）安全地合并成最终的payload。
SUMMARY_PAYLOAD=$(jq -n \
  --argjson base "$SUMMARY_PAYLOAD_BASE" \
  --argjson sample "$FAILED_URLS_SAMPLE" \
  '$base + {{"summary_failed_urls": $sample}}')

# echo "SUMMARY_PAYLOAD = $SUMMARY_PAYLOAD"

echo "--- DEBUG 4: Start loging final summary and cleaning ---"

log_to_gcp "Task finished. Final summary below." "NOTICE" "$SUMMARY_PAYLOAD"

# --- 清理工作 ---
# 删除临时文件。
# rm -rf "$RESULTS_DIR" "$URL_LIST_FILE"
log_to_gcp "Cleanup complete. Script finished." "INFO" '{{"vm_name": "'$(hostname)'", "region": "{region}", "task_id":"{shard_suffix}"}}'

# --- (可选) 任务完成后自动销毁虚拟机 ---
# 如果需要，可以取消下面这行的注释。请确保服务账号有删除实例的权限。
# gcloud compute instances delete "$(hostname)" --zone="{{zone}}" --quiet
'''





# --- 循环创建10台工作虚拟机 ---
def create_vm(region, type):
    # 3. 循环创建虚拟机
    # 这个循环会根据num_vms配置的数量来执行
    for i in range(int(num_vms)):
        # 从动态资源的输出中获取对应分片的GCS路径
        # .apply() 用于处理在部署时才能知道的输出值 (Output<T>)
        gcs_shard_path_output = url_shards.shard_paths.apply(lambda paths, i=i: paths[i])
        # 准备启动脚本模板
        # 我们使用 .apply() 来将动态获取的路径安全地插入脚本

        vm_name = f"prewarm-worker-{region}-{i:02d}"

        # ★★★ 这是关键的修改 ★★★
        # 我们将 .format() 方法移动到 gcs_shard_path_output.apply() 的lambda函数内部
        # 这样，当lambda函数执行时，'path'就是一个普通的字符串了
        startup_script = gcs_shard_path_output.apply(
            lambda path, i=i: startup_script_template.format(
                # 这里 'path' 是从Output对象解析出来的真实GCS路径字符串
                gcs_url_list_path=path, 
                
                # 'region' 和 'shard_suffix' 是普通的Python变量，可以直接在lambda函数中使用
                region=region,
                shard_suffix=f"{i:02d}",
                total_attempts=total_attempts
            )
        )

        instance = gcp.compute.Instance(resource_name=vm_name,
            machine_type=type,
            zone=region+"-b",
            boot_disk=gcp.compute.InstanceBootDiskArgs(
                initialize_params=gcp.compute.InstanceBootDiskInitializeParamsArgs(
                    image="debian-cloud/debian-11",
                    size=30 # 适当增加磁盘大小
                ),
            ),
            network_interfaces=[gcp.compute.InstanceNetworkInterfaceArgs(
                network="default",
                access_configs=[gcp.compute.InstanceNetworkInterfaceAccessConfigArgs()],
            )],
            metadata={"startup-script": startup_script},
            service_account=gcp.compute.InstanceServiceAccountArgs(
                email=config.require('service_account_email'),
                scopes=["cloud-platform"],
            ),
            # 在这里应用自定义超时选项
            opts=pulumi.ResourceOptions(custom_timeouts=long_timeout, depends_on=[url_shards]) # 确保虚拟机在文件分割和上传完成后再创建
        )
        pulumi.export("shard_file_paths", url_shards.shard_paths)
        pulumi.export('instance_name', instance.name)

exclude_regions = ["me-central2"]
region_list = gcp.compute.get_regions()

# for region in region_list.names:
#     # 在创建VM之前，检查当前区域是否在排除列表中
#     if region not in exclude_regions:
#         create_vm(region, type)
#     else:
#         # (可选) 打印一条信息，让你知道哪个区域被跳过了
#         print(f"Skipping region {region} as it is in the exclusion list.")

# create_vm("us-central1", type)
create_vm("europe-north1", type)
# create_vm("asia-east1", type)

pulumi.export("message", f"Scheduled creation for {num_vms} worker VMs.")
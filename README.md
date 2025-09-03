# gcp-cdnprewarm

## Setup
```
alias p=pulumi
p login
p config set gcp:project [your_project_id]
```

## Create your own stack
```
p stack select
```

## Create a new Pulumi config file: Pulumi.[your_stack].yaml
```
config:
  cdn_prewarm:type: e2-standard-4
  cdn_prewarm:num_vms: [可自定义] # 改为自定义的机器数量
  cdn_prewarm:total_attempts: [可自定义] # 改为对每个URL请求的次数
  cdn_prewarm:gcs_bucket_name: [your-bucket-name] # 改为存储桶名称
  cdn_prewarm:gcs_url_list_path: "[your-bucket-dir-name]/[your-url-list-file path]" # 改为源文件在桶内的路径
  cdn_prewarm:gcs_shard_output_dir: "[your-bucket-dir-name]" # 在桶内的存放分片的目录
  cdn_prewarm:service_account_email: sa-vm-prewarm-manual@[your-project-id].iam.gserviceaccount.com # 改为所创建的服务账号
  gcp:project: [your-project-id] # 改为项目id
```

## Run
```
p up -y --parallel [custom number like 10] -s [your_stack]
```

## Clean
```
p destroy -s [your_stack] -y
```

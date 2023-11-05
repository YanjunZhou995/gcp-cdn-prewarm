# gcp-cdnprewarm

## Setup
```
alias p=pulumi
p login
p config set gcp:project your_project_id
```

## Modify url.txt
add your urls to url.txt

## Run
```
p up -s dev -y
```

## Clean
```
p destroy -s dev -y
```

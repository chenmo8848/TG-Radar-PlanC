# TG-Radar-PlanC 一键安装命令

先把这两个文件上传到仓库根目录：

- `get.sh`
- 替换后的 `install.sh`

然后在 README 里放这条命令：

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/chenmo8848/TG-Radar-PlanC/main/get.sh)
```

也可以指定安装目录：

```bash
TGRC_INSTALL_DIR=/opt/TGRC bash <(curl -fsSL https://raw.githubusercontent.com/chenmo8848/TG-Radar-PlanC/main/get.sh)
```

也可以指定分支：

```bash
TGRC_BRANCH=main bash <(curl -fsSL https://raw.githubusercontent.com/chenmo8848/TG-Radar-PlanC/main/get.sh)
```

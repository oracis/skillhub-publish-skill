# Windows / Git Bash 路径与 slug 陷阱

这几个坑都会**静默失败**（发布成功、但你要的效果没发生），且都在本机真实踩过。

## 坑 1：Git Bash 把 `C:/...` 改写成 `/c/...`

### 现象

```bash
python publish.py publish ./my-skill --icon C:/x/icon.png
# WARN: --icon C:/x/icon.png 不存在，跳过图标上传     ← 你以为传成功了
# 发布返回 201 success:true
# 但 iconAuditStatus 是 null，什么都没发生
```

真相：**MSYS/Git Bash 在把参数交给 `python.exe` 前，会把 `C:/x/icon.png` 自动
翻译成 `/c/x/icon.png`**。Python 在 Windows 上不认 `/c/...`，于是 `os.path.exists` 返回 False。

最阴的地方：`--icon` 找不到只打一句 **WARN 到 stderr**、不阻断发布（这是有意的设计，
宁可少个图标也别让发布失败）。所以 stdout 里是干净的 `success: true`，
`WARN` 容易被 grep 漏掉 → 你以为图标更新了，其实没有。

### 判据

发布响应里看 `iconAuditStatus`：

- `"pending"` / `"approved"` → 真的传上去了
- `"null"` → **没传上去**，回去看 stderr 有没有 `WARN: --icon ... 不存在`

### 规避（已内置）

`publish.py` 的 `normalize_path()` 会自动把 `/c/xxx` 还原成 `C:/xxx`，
`skill_dir` 与 `--icon` 都过这道归一化。**但脚本外自己写 curl / 其他工具时没有这层保护**，
要么用 `C:/` 且确认没被翻译，要么用相对路径。

## 坑 2：slug 自愈会**改写你的 SKILL.md**

### 现象

`publish.py` 有个「slug 冲突自愈」功能：409/500 说 slug 被占用时，
会自动试 `wb-<slug>` 等后备名，**成功后就地把新 slug 回写进 SKILL.md**。

这个「贴心」功能有副作用：

```yaml
# 原本
slug: aliyun-oss-static-deploy-skill
# 某次自愈成功后，文件里被改成了
slug: wb-aliyun-oss-static-deploy-skill
```

此后每次发布会打到 `wb-...` 这个**新条目**上，而原条目再也不更新 ——
表现为「我明明发了更新，线上还是旧版」，或者后台莫名多出一个重复技能。

### 判据

```bash
grep -n "^slug:" <skill目录>/SKILL.md     # 和目录名 / 期望值一致吗？
python publish.py mine                    # 有没有多出 wb- / -skill 后缀的重复条目
```

发布响应里也可看 `slugUsed` —— **它和你预期不一致就说明发生了自愈**。

### 处置

1. 把 SKILL.md 里的 `slug:` 改回正确值
2. 升版本号（线上已被自愈的那次占用了旧号）
3. 重新发布到正确 slug
4. 用 `publish.py rm <错slug> --yes` 删掉重复条目

### 预防

- 发布前先 `grep "^slug:"` 对一眼
- 看到 `slugUsed` 与预期不符，立刻停下检查，别继续发
- 想彻底避免：**首次发布就用最终确定的 slug**，并且不要让它落到「被占用」的分支

## 顺带：`displayName` 必须是**字符串**，不能是嵌套 map

有些技能的 frontmatter 写成：

```yaml
displayName:
  zh: 某技能
  en: Some Skill
```

本脚本的简易 frontmatter 解析器**不支持嵌套**，会把它解析成空串 + 两个散落键
（`zh` / `en`），于是 `validate` 报「缺必填字段：displayName」。

改成单行字符串即可：

```yaml
displayName: 某技能
```

平台只认一个展示名，嵌套形式的 `en` 本来也不会被用到。

## 坑 3：图标文件不在技能目录里 → `iconAuditStatus: null`

### 现象

```bash
python publish.py publish ./skillhub-publish-skill     # 返回 success: true
# 但响应里 "iconAuditStatus": null                     ← 图标根本没提交
```

**且 stdout 里连 WARN 都没有**（因为 `--icon` 没传，`find_icon()` 走的是自动兜底，
一个候选都没找到就静默返回 None）。

### 根因

`find_icon()` 的查找顺序是：

1. `--icon` 显式指定
2. `cover.png/jpg/webp`、`icon.png/jpg/webp`
3. `<目录名>.png/jpg/webp`（本技能新增的兜底）

第 3 条要求**图标文件名严格等于技能目录名**。此前图标是临时生成在别处、
`--icon` 指过去传的，**文件从没落进技能目录** → 下次不带 `--icon` 发版，
图标就丢了，而且完全无声。

### 判据

- 发布响应 `iconAuditStatus` 为 `null` → 没提交（见坑 1 判据）
- `ls <技能目录>/*.png` 为空 → 根因在此

### 处置 / 预防

**把 `<slug>.png` 常驻放在技能目录根部**，之后每次发版都会自动带上：

```bash
python ~/.workbuddy/skills/zero-dep-icon-gen/scripts/gen_icons.py \
    --spec "my-skill:#4F46E5,#818CF8,browser" -o my-skill/
```

包里不会因此变大 —— 位图在 `collect_bundle()` 阶段已被排除
（服务端拒收位图，必须走 `skill-icons/upload` 单独上传），它只是本地素材。

## 坑 4：`git push origin master` 报 `src refspec does not match any`

### 现象

```bash
git push origin master
# error: src refspec master does not match any
```

或：

```bash
git status -sb
# ## main...origin/main [gone]      ← 上游引用"消失"了
```

### 根因

新建仓库时 GitHub 默认分支是 **`main`**，但命令行习惯容易写成 `master`。
第二种 `[gone]` 出现在**远程分支被改过名**、或本地 remote 缓存失效时；
此时 `git push` 不带 refspec 也会报 `no upstream`。

### 处置

```bash
# 1. 先问远程到底有什么分支，别猜
git ls-remote origin | head

# 2. 按实际分支名推
git push origin main

# 3. 若 upstream 丢了，重新绑定
git push -u origin main        # 重新设置 upstream 并推
# 或
git branch --set-upstream-to=origin/main main
```

**别直接 `git push --force`** —— 先 `git ls-remote` 看清远程状态，
本机就曾因盲目强推差点覆盖别人（或自己早先）的提交。

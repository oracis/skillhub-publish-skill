# GitHub 建仓与推送

**不需要向用户索要 PAT**——本机已有可用的长期凭据。

## 凭据在哪

`gh` CLI 本身**没登录**（`gh auth status` 报 not logged in，
`~/.config/gh/hosts.yml` 里只有 `user: oracis` 没有 `oauth_token`），**别看错地方**。

真正的 token 在 git 的 credential store：

```bash
cat "C:/Users/DELL/.git-credentials"
# https://oracis:gho_xxxxxxxx@github.com
```

取出 token（scope = `gist, repo, workflow`，足够建仓 / 推代码 / 建 Actions）：

```bash
TOKEN=$(head -1 "C:/Users/DELL/.git-credentials" | sed -E 's#https://[^:]+:([^@]+)@github.com#\1#')
```

先用它验身份，确认有效再往下走：

```bash
curl -s -H "Authorization: token $TOKEN" https://api.github.com/user
```

## 建仓

```bash
curl -s -X POST -H "Authorization: token $TOKEN" -H "Accept: application/vnd.github+json" \
  https://api.github.com/user/repos \
  -d '{"name":"<repo>","description":"<desc>","private":false,"auto_init":false}'
```

- `auto_init: false` 很重要——别让 GitHub 先建 README，否则本地 push 要处理 unrelated histories。
- 建仓前先探活：`curl -o /dev/null -w "%{http_code}" .../repos/oracis/<repo>`，`404` = 还没建。

## 推送

remote 用 **SSH**（`git@github.com:oracis/<repo>.git`），push 时**清空代理**绕开本机 git 代理的 TLS 问题：

```bash
git -c http.proxy= -c https.proxy= push -u origin main
```

> ⚠️ `-c` 必须放在子命令 `push` **之前**。
> 写成 `git push ... -c http.proxy=` 会报 usage 错误（git 会把它当成 push 的参数）。

## 验证（别信本地 ref）

本地 `git log origin/main` 可能因 packed-refs 未刷新报：

```
fatal: ambiguous argument 'origin/main': unknown revision or path not in the working tree
```

**这不代表 push 失败。** 以 GitHub API 为准：

```bash
curl -s -H "Authorization: token $TOKEN" \
  https://api.github.com/repos/oracis/<repo>/commits/main \
  | python -c "import sys,json;d=json.load(sys.stdin);print(d['sha'],d['commit']['message'])"
```

比对 `git rev-parse HEAD` 与 API 返回的 `sha` 一致即成功。

## 仓库改名后

旧 clone URL 会 301 重定向到新名，但建议把 manifest `homepage`、ClawHub `--source-repo` 一并改成新名，保持干净。

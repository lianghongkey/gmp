// ethaxi_run —— 带 CAP_NET_RAW 的启动器。
//   一次性授权：  sudo setcap cap_net_raw+eip build/ethaxi_run
//   用法：        build/ethaxi_run python host/soc_generate.py "今天天气不错，我们去"
//   只肯启动两类目标：发布包目录之下的可执行文件，以及「python 加一个包内脚本」。
#define _GNU_SOURCE
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <linux/capability.h>

#ifndef PR_CAP_AMBIENT
#define PR_CAP_AMBIENT 47
#define PR_CAP_AMBIENT_RAISE 2
#endif

static int under(const char *path, const char *root) {
    char real[PATH_MAX];
    if (!realpath(path, real)) return 0;
    size_t n = strlen(root);
    return strncmp(real, root, n) == 0 && (real[n] == '/' || real[n] == '\0');
}

// 在 PATH 里找 prog（execvp 的规则），得到 realpath
static int resolve_prog(const char *prog, char *out) {
    if (strchr(prog, '/')) return realpath(prog, out) != NULL;
    const char *path = getenv("PATH");
    if (!path) return 0;
    char buf[PATH_MAX];
    while (*path) {
        const char *sep = strchr(path, ':');
        size_t n = sep ? (size_t)(sep - path) : strlen(path);
        if (n && n + 1 + strlen(prog) < sizeof buf) {
            memcpy(buf, path, n); buf[n] = '/'; strcpy(buf + n + 1, prog);
            if (access(buf, X_OK) == 0 && realpath(buf, out)) return 1;
        }
        if (!sep) break;
        path = sep + 1;
    }
    return 0;
}

static int is_python(const char *real) {
    const char *base = strrchr(real, '/'); base = base ? base + 1 : real;
    return strncmp(base, "python", 6) == 0;
}

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "用法: %s <程序> [参数...]\n", argv[0]); return 2; }

    // 发布包根目录 = 自身位置往上 4 级（host/ethaxi/build/ethaxi_run）
    char self[PATH_MAX], root[PATH_MAX];
    ssize_t k = readlink("/proc/self/exe", self, sizeof self - 1);
    if (k < 0) { perror("readlink /proc/self/exe"); return 2; }
    self[k] = 0;
    strcpy(root, self);
    for (int i = 0; i < 4; i++) { char *s = strrchr(root, '/'); if (!s) { fprintf(stderr, "推不出发布包根目录\n"); return 2; } *s = 0; }

    char prog[PATH_MAX];
    if (!resolve_prog(argv[1], prog)) { fprintf(stderr, "找不到程序 %s\n", argv[1]); return 2; }
    int ok = 0;
    if (under(prog, root)) ok = 1;
    else if (is_python(prog) && argc >= 3 && under(argv[2], root)) ok = 1;
    if (!ok) {
        fprintf(stderr, "ethaxi_run 拒绝：%s 不在 %s 之下（或不是「python <包内脚本>」）\n", prog, root);
        return 3;
    }

    // inheritable |= CAP_NET_RAW（permitted / effective 原样保留）
    struct __user_cap_header_struct hdr = { _LINUX_CAPABILITY_VERSION_3, 0 };
    struct __user_cap_data_struct data[2];
    if (syscall(SYS_capget, &hdr, data) != 0) { perror("capget"); return 2; }
    data[CAP_TO_INDEX(CAP_NET_RAW)].inheritable |= CAP_TO_MASK(CAP_NET_RAW);
    if (syscall(SYS_capset, &hdr, data) != 0) {
        fprintf(stderr, "把 CAP_NET_RAW 加进 inheritable 失败：%s\n  本文件需要 file capability：sudo setcap cap_net_raw+eip %s\n",
                strerror(errno), self);
        return 2;
    }
    if (prctl(PR_CAP_AMBIENT, PR_CAP_AMBIENT_RAISE, CAP_NET_RAW, 0, 0) != 0) {
        fprintf(stderr, "提升 CAP_NET_RAW 为 ambient 失败：%s\n  本文件需要 file capability：sudo setcap cap_net_raw+eip %s\n",
                strerror(errno), self);
        return 2;
    }
    execv(prog, argv + 1);
    perror("execv");
    return 2;
}

/* The camera's BusyBox has no setsid applet. Use the ARM Linux EABI directly. */
enum {
    SYS_FORK = 2,
    SYS_OPEN = 5,
    SYS_CLOSE = 6,
    SYS_EXECVE = 11,
    SYS_DUP2 = 63,
    SYS_SETSID = 66,
    O_WRONLY = 1,
    O_CREAT = 64,
    O_APPEND = 1024,
};

static int syscall3(int nr, int a, int b, int c)
{
    register int r0 __asm__("r0") = a;
    register int r1 __asm__("r1") = b;
    register int r2 __asm__("r2") = c;
    register int r7 __asm__("r7") = nr;
    __asm__ volatile("svc 0" : "+r"(r0) : "r"(r1), "r"(r2), "r"(r7) : "memory", "cc");
    return r0;
}

static int equal(const char *a, const char *b)
{
    while (*a && *a == *b) {
        a++;
        b++;
    }
    return *a == *b;
}

int main(int argc, char **argv)
{
    if (argc != 4 || !equal(argv[1], "detach-script") || argv[2][0] != '/' || argv[3][0] != '/')
        return 2;

    int child = syscall3(SYS_FORK, 0, 0, 0);
    if (child != 0)
        return child < 0;
    if (syscall3(SYS_SETSID, 0, 0, 0) < 0)
        return 1;
    child = syscall3(SYS_FORK, 0, 0, 0);
    if (child != 0)
        return child < 0;

    int input = syscall3(SYS_OPEN, (int)"/dev/null", 0, 0);
    int log = syscall3(SYS_OPEN, (int)argv[3], O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (input < 0 || log < 0 || syscall3(SYS_DUP2, input, 0, 0) < 0 ||
        syscall3(SYS_DUP2, log, 1, 0) < 0 || syscall3(SYS_DUP2, log, 2, 0) < 0)
        return 1;

    for (int fd = 3; fd < 1024; fd++)
        syscall3(SYS_CLOSE, fd, 0, 0);
    char *args[] = {"sh", argv[2], 0};
    char *env[] = {"PATH=/bin:/sbin:/usr/bin:/usr/sbin", 0};
    syscall3(SYS_EXECVE, (int)"/bin/sh", (int)args, (int)env);
    return 1;
}

__asm__(".global _start\n"
        "_start:\n"
        "ldr r0,[sp]\n"
        "add r1,sp,#4\n"
        "bl main\n"
        "mov r7,#1\n"
        "svc 0\n");

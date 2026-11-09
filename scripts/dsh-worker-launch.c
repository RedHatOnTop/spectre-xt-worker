#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>

#ifndef PYTHON
#define PYTHON "/usr/bin/python3"
#endif

#ifndef WORKER_SCRIPT
#define WORKER_SCRIPT "/usr/local/lib/spectre-superagent/spectre-dsh-worker.py"
#endif

#ifdef WORKER_ROOT
#include <sys/stat.h>

static int carrier_script(const char *path) {
    size_t length = strlen(path);
    if (path[0] != '/' || length < 4 || strcmp(path + length - 3, ".py") != 0) {
        return 0;
    }
    if (strstr(path, "/../") != NULL || strstr(path, "/scripts/") == NULL) {
        return 0;
    }
    struct stat info;
    return stat(path, &info) == 0 && S_ISREG(info.st_mode);
}

static int lookup_script(const char *route, char *script, size_t script_len) {
    char map_path[4096];
    int written = snprintf(map_path, sizeof map_path, "%s/carrier-map", WORKER_ROOT);
    if (written < 0 || (size_t)written >= sizeof map_path) {
        fputs("dsh-worker: carrier map path is too long\n", stderr);
        return -1;
    }
    FILE *file = fopen(map_path, "r");
    if (file == NULL) {
        perror("dsh-worker: cannot read carrier map");
        return -1;
    }
    char line[8192];
    int found = 0;
    while (fgets(line, sizeof line, file) != NULL) {
        size_t length = strlen(line);
        if (length == sizeof line - 1 && line[length - 1] != '\n') {
            fputs("dsh-worker: carrier map line is too long\n", stderr);
            fclose(file);
            return -1;
        }
        if (length > 0 && line[length - 1] == '\n') {
            line[--length] = '\0';
        }
        if (length == 0 || line[0] == '#') {
            continue;
        }
        char *space = strchr(line, ' ');
        if (space == NULL || strchr(space + 1, ' ') != NULL) {
            fputs("dsh-worker: carrier map line is malformed\n", stderr);
            fclose(file);
            return -1;
        }
        *space = '\0';
        if (strcmp(line, route) != 0) {
            continue;
        }
        if (found || strlen(space + 1) + 1 > script_len || !carrier_script(space + 1)) {
            fputs("dsh-worker: carrier script must be one absolute Python file under scripts/\n", stderr);
            fclose(file);
            return -1;
        }
        memcpy(script, space + 1, strlen(space + 1) + 1);
        found = 1;
    }
    fclose(file);
    if (!found) {
        fputs("dsh-worker: carrier map has no script for this route\n", stderr);
        return -1;
    }
    return 0;
}

static int launch_mapped(const char *route) {
    char script[4096];
    if (lookup_script(route, script, sizeof script) != 0) {
        return 127;
    }
    execl(PYTHON, "python3", script, "--route", route, (char *)NULL);
    perror("dsh-worker: cannot start native worker protocol");
    return 127;
}
#else
#include <signal.h>
#include <sys/wait.h>

static volatile sig_atomic_t worker_pid;

static void request_shutdown(int signal_number) {
    int saved_errno = errno;
    if (worker_pid > 0) {
        kill((pid_t)worker_pid, signal_number);
    }
    errno = saved_errno;
}

static int launch_forked(const char *route) {
    struct sigaction action = {0};
    action.sa_handler = request_shutdown;
    if (sigemptyset(&action.sa_mask) < 0 || sigaction(SIGTERM, &action, NULL) < 0 ||
        sigaction(SIGINT, &action, NULL) < 0 || sigaction(SIGHUP, &action, NULL) < 0) {
        perror("dsh-worker: cannot prepare process lifecycle");
        return 127;
    }
    pid_t child = fork();
    if (child < 0) {
        perror("dsh-worker: cannot create execution process");
        return 127;
    }
    if (child == 0) {
        execl(PYTHON, "python3", WORKER_SCRIPT, "--route", route, (char *)NULL);
        perror("dsh-worker: cannot start native worker protocol");
        _exit(127);
    }
    worker_pid = child;
    int status;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) {
            perror("dsh-worker: cannot record worker process exit");
            return 127;
        }
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    fputs("dsh-worker: unknown worker wait status\n", stderr);
    return 127;
}
#endif

int main(int argc, char **argv) {
    if (argc != 1) {
        fputs("dsh-worker: launch accepts no arguments; the binary pins its route\n", stderr);
        return 2;
    }
    const char *name = strrchr(argv[0], '/');
    name = name == NULL ? argv[0] : name + 1;
    const char *route;
    if (strcmp(name, "dsh-worker") == 0) {
        route = "clinepass";
    } else if (strcmp(name, "dsh-worker-go") == 0) {
        route = "opencode-go";
    } else {
        fputs("dsh-worker: unknown route-specific launcher name\n", stderr);
        return 2;
    }
#ifdef WORKER_ROOT
    return launch_mapped(route);
#else
    return launch_forked(route);
#endif
}

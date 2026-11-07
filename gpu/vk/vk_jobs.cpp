// The worker threads (vk_jobs.h). As many as a third of the hardware threads (2 to 4): the game itself runs six
// virtual cores, and the replay thread is one of the busiest.
#include <algorithm>
#include <condition_variable>
#include <deque>
#include <mutex>
#include <thread>
#include <vector>

#include "vk_jobs.h"
#include "vk_internal.h"

namespace mkwvk {
namespace {

std::mutex mu;
std::condition_variable work_cv, done_cv;
// (hookshot) each job with the flag it sets, if any: a thread waiting on that flag takes its own job while it is still queued
struct Item {
    std::function<void()> fn;
    const std::atomic<bool>* flag;
};
std::deque<Item> queue;
std::atomic<int> pending{0};
bool started = false;

void worker() {
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL);
    SetThreadDescription(GetCurrentThread(), L"mkw_vk worker");
    for (;;) {
        std::function<void()> fn;
        {
            std::unique_lock lk(mu);
            work_cv.wait(lk, [] { return !queue.empty(); });
            fn = std::move(queue.front().fn);
            queue.pop_front();
        }
        fn();
        pending--;
        jobs_notify();
    }
}

void start() {
    const unsigned n = std::clamp(std::thread::hardware_concurrency() / 3, 2u, 4u);
    for (unsigned i = 0; i < n; i++) std::thread(worker).detach();
    started = true;
    logf("vk: %u background workers (shader translation, pipelines)", n);
}

} // namespace

void jobs_push(std::function<void()> fn, bool urgent, const std::atomic<bool>* flag) {
    std::lock_guard lk(mu);
    if (!started) start();
    pending++;
    if (urgent) queue.push_front({std::move(fn), flag});
    else queue.push_back({std::move(fn), flag});
    work_cv.notify_one();
}
void jobs_notify() {
    std::lock_guard lk(mu);
    done_cv.notify_all();
}
void jobs_wait(const std::atomic<bool>& flag) {
    std::unique_lock lk(mu);
    // (hookshot) its job not started yet: run here, on the waiting thread, rather than idle behind the workers' queue.
    // A first-seen area waited on each of its new programs in turn, 30 to 50 ms apiece, while the workers were busy
    for (auto it = queue.begin(); it != queue.end(); ++it)
        if (it->flag == &flag) {
            std::function<void()> fn = std::move(it->fn);
            queue.erase(it);
            lk.unlock();
            fn();
            pending--;
            jobs_notify();
            return;
        }
    done_cv.wait(lk, [&] { return flag.load(std::memory_order_acquire); });
}
int jobs_pending() {
    return pending.load();
}

} // namespace mkwvk

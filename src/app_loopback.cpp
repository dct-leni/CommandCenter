#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <roapi.h>
#include <audioclient.h>
#include <audioclientactivationparams.h>
#include <mmdeviceapi.h>
#include <io.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>

#pragma comment(lib, "ole32.lib")
#pragma comment(lib, "runtimeobject.lib")
#pragma comment(lib, "mmdevapi.lib")
#pragma comment(lib, "avrt.lib")

static const IID IID_IAgileObject = { 0x94ea2b94, 0xe9cc, 0x49e0, { 0xc0, 0xff, 0xee, 0x64, 0xca, 0x8f, 0x5b, 0x90 } };

class LoopbackActivator : public IActivateAudioInterfaceCompletionHandler, public IAgileObject
{
    LONG m_refCount;
    HANDLE m_hEvent;
    HRESULT m_hr;
    IAudioClient* m_pAudioClient;

public:
    LoopbackActivator() : m_refCount(1), m_hr(E_FAIL), m_pAudioClient(nullptr) {
        m_hEvent = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    }
    ~LoopbackActivator() {
        if (m_hEvent) CloseHandle(m_hEvent);
    }

    STDMETHODIMP QueryInterface(REFIID riid, void** ppv) override {
        if (!ppv) return E_POINTER;
        if (riid == __uuidof(IUnknown) || riid == __uuidof(IActivateAudioInterfaceCompletionHandler)) {
            *ppv = static_cast<IActivateAudioInterfaceCompletionHandler*>(this);
            AddRef();
            return S_OK;
        }
        if (riid == IID_IAgileObject) {
            *ppv = static_cast<IAgileObject*>(this);
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }

    STDMETHODIMP_(ULONG) AddRef() override {
        return InterlockedIncrement(&m_refCount);
    }

    STDMETHODIMP_(ULONG) Release() override {
        LONG count = InterlockedDecrement(&m_refCount);
        if (count == 0) delete this;
        return count;
    }

    STDMETHODIMP ActivateCompleted(IActivateAudioInterfaceAsyncOperation* activateOperation) override {
        HRESULT hrActivate = E_FAIL;
        IUnknown* pUnknown = nullptr;
        if (activateOperation) {
            m_hr = activateOperation->GetActivateResult(&hrActivate, &pUnknown);
            if (SUCCEEDED(m_hr) && SUCCEEDED(hrActivate) && pUnknown) {
                m_hr = pUnknown->QueryInterface(__uuidof(IAudioClient), (void**)&m_pAudioClient);
                pUnknown->Release();
            }
        }
        SetEvent(m_hEvent);
        return S_OK;
    }

    HRESULT WaitForCompletion(DWORD timeoutMs, IAudioClient** ppClient) {
        WaitForSingleObject(m_hEvent, timeoutMs);
        if (SUCCEEDED(m_hr) && m_pAudioClient) {
            *ppClient = m_pAudioClient;
            return S_OK;
        }
        return m_hr;
    }
};

int main(int argc, char* argv[]) {
    if (argc < 2) {
        fprintf(stderr, "Usage: app_loopback.exe <PID> [SampleRate]\n");
        return 1;
    }

    DWORD targetPid = (DWORD)strtoul(argv[1], nullptr, 10);
    DWORD sampleRate = 48000;
    if (argc >= 3) {
        sampleRate = (DWORD)strtoul(argv[2], nullptr, 10);
        if (sampleRate == 0) sampleRate = 48000;
    }

    _setmode(_fileno(stdout), _O_BINARY);
    RoInitialize(RO_INIT_MULTITHREADED);

    AUDIOCLIENT_ACTIVATION_PARAMS params = {};
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE;
    params.ProcessLoopbackParams.TargetProcessId = targetPid;

    PROPVARIANT prop = {};
    prop.vt = VT_BLOB;
    prop.blob.cbSize = sizeof(params);
    prop.blob.pBlobData = (BYTE*)&params;

    LoopbackActivator* activator = new LoopbackActivator();
    IActivateAudioInterfaceAsyncOperation* asyncOp = nullptr;

    HRESULT hr = ActivateAudioInterfaceAsync(
        VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
        __uuidof(IAudioClient),
        &prop,
        activator,
        &asyncOp
    );

    if (FAILED(hr)) {
        fprintf(stderr, "ActivateAudioInterfaceAsync failed: 0x%08X\n", hr);
        activator->Release();
        RoUninitialize();
        return 1;
    }

    IAudioClient* pAudioClient = nullptr;
    hr = activator->WaitForCompletion(5000, &pAudioClient);
    if (asyncOp) asyncOp->Release();
    activator->Release();

    if (FAILED(hr) || !pAudioClient) {
        fprintf(stderr, "WaitForCompletion failed: 0x%08X\n", hr);
        RoUninitialize();
        return 1;
    }

    WAVEFORMATEX wfx = {};
    wfx.wFormatTag = WAVE_FORMAT_PCM;
    wfx.nChannels = 2;
    wfx.nSamplesPerSec = sampleRate;
    wfx.wBitsPerSample = 16;
    wfx.nBlockAlign = (wfx.nChannels * wfx.wBitsPerSample) / 8;
    wfx.nAvgBytesPerSec = wfx.nSamplesPerSec * wfx.nBlockAlign;

    hr = pAudioClient->Initialize(
        AUDCLNT_SHAREMODE_SHARED,
        AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
        0, 0, &wfx, nullptr
    );

    if (FAILED(hr)) {
        fprintf(stderr, "AudioClient Initialize failed: 0x%08X\n", hr);
        pAudioClient->Release();
        RoUninitialize();
        return 1;
    }

    HANDLE hSampleEvent = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    pAudioClient->SetEventHandle(hSampleEvent);

    IAudioCaptureClient* pCapture = nullptr;
    hr = pAudioClient->GetService(__uuidof(IAudioCaptureClient), (void**)&pCapture);
    if (FAILED(hr)) {
        fprintf(stderr, "GetService IAudioCaptureClient failed: 0x%08X\n", hr);
        CloseHandle(hSampleEvent);
        pAudioClient->Release();
        RoUninitialize();
        return 1;
    }

    HANDLE hTargetProc = OpenProcess(SYNCHRONIZE, FALSE, targetPid);

    hr = pAudioClient->Start();
    if (FAILED(hr)) {
        fprintf(stderr, "AudioClient Start failed: 0x%08X\n", hr);
    } else {
        fprintf(stderr, "Streaming PCM 16-bit %luHz Stereo for PID %lu...\n", sampleRate, targetPid);
    }

    HANDLE waitHandles[2] = { hSampleEvent, hTargetProc };
    DWORD handleCount = hTargetProc ? 2 : 1;

    static const BYTE silenceBuf[4096] = {0};
    bool running = true;

    while (running) {
        DWORD waitRes = WaitForMultipleObjects(handleCount, waitHandles, FALSE, 20);
        if (waitRes == WAIT_OBJECT_0 || waitRes == WAIT_TIMEOUT) {
            UINT32 nextPacketSize = 0;
            bool gotData = false;

            while (SUCCEEDED(pCapture->GetNextPacketSize(&nextPacketSize)) && nextPacketSize > 0) {
                BYTE* pData = nullptr;
                UINT32 numFrames = 0;
                DWORD flags = 0;
                
                hr = pCapture->GetBuffer(&pData, &numFrames, &flags, nullptr, nullptr);
                if (SUCCEEDED(hr) && numFrames > 0) {
                    gotData = true;
                    if (flags & AUDCLNT_BUFFERFLAGS_SILENT) {
                        size_t bytesToWrite = numFrames * wfx.nBlockAlign;
                        while (bytesToWrite > 0) {
                            size_t chunk = bytesToWrite > sizeof(silenceBuf) ? sizeof(silenceBuf) : bytesToWrite;
                            size_t written = fwrite(silenceBuf, 1, chunk, stdout);
                            if (written == 0 && ferror(stdout)) { running = false; break; }
                            bytesToWrite -= chunk;
                        }
                    } else {
                        size_t written = fwrite(pData, 1, numFrames * wfx.nBlockAlign, stdout);
                        if (written == 0 && ferror(stdout)) { running = false; break; }
                    }
                    pCapture->ReleaseBuffer(numFrames);
                } else {
                    break;
                }
            }

            // On timeout (silence gap where browser produces no audio events for 20ms):
            // Fill 20ms of silence (20 * sampleRate / 1000 frames) to keep FFmpeg PCM timeline continuous
            if (waitRes == WAIT_TIMEOUT && !gotData) {
                size_t silenceFrames = (20 * sampleRate) / 1000;
                size_t bytesToWrite = silenceFrames * wfx.nBlockAlign;
                while (bytesToWrite > 0) {
                    size_t chunk = bytesToWrite > sizeof(silenceBuf) ? sizeof(silenceBuf) : bytesToWrite;
                    size_t written = fwrite(silenceBuf, 1, chunk, stdout);
                    if (written == 0 && ferror(stdout)) { running = false; break; }
                    bytesToWrite -= chunk;
                }
            }

            fflush(stdout);
        } else if (handleCount > 1 && waitRes == WAIT_OBJECT_0 + 1) {
            fprintf(stderr, "Target PID %lu exited, closing audio capture.\n", targetPid);
            running = false;
        }
    }

    pAudioClient->Stop();
    if (hTargetProc) CloseHandle(hTargetProc);
    pCapture->Release();
    CloseHandle(hSampleEvent);
    pAudioClient->Release();
    RoUninitialize();
    return 0;
}

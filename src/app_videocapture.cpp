
#define NOMINMAX
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <timeapi.h>
#include <windows.graphics.capture.interop.h>
#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Foundation.Collections.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.h>
#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>
#include <d3d11.h>
#include <dxgi.h>
#include <dxgi1_2.h>
#include <windows.graphics.directx.direct3d11.interop.h>
#include <io.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string>
#include <vector>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <thread>

#pragma comment(lib, "d3d11.lib")
#pragma comment(lib, "dxgi.lib")
#pragma comment(lib, "windowsapp.lib")
#pragma comment(lib, "user32.lib")
#pragma comment(lib, "winmm.lib")

namespace winrt_cap = winrt::Windows::Graphics::Capture;
namespace winrt_dx = winrt::Windows::Graphics::DirectX;
namespace winrt_d3d = winrt::Windows::Graphics::DirectX::Direct3D11;

std::atomic<bool> g_running{ true };

BOOL WINAPI ConsoleHandler(DWORD ctrlType) {
    if (ctrlType == CTRL_C_EVENT || ctrlType == CTRL_CLOSE_EVENT) {
        g_running = false;
        return TRUE;
    }
    return FALSE;
}

int main(int argc, char* argv[]) {
    HWND targetHwnd = nullptr;
    int targetFps = 30;
    int reqWidth = 0;
    int reqHeight = 0;
    std::wstring pipePath = L"";

    for (int i = 1; i < argc; i++) {
        std::string arg = argv[i];
        if (arg == "--hwnd" && i + 1 < argc) {
            std::string val = argv[++i];
            targetHwnd = (HWND)std::stoull(val, nullptr, 0);
        } else if (arg == "--fps" && i + 1 < argc) {
            targetFps = std::stoi(argv[++i]);
        } else if (arg == "--width" && i + 1 < argc) {
            reqWidth = std::stoi(argv[++i]);
        } else if (arg == "--height" && i + 1 < argc) {
            reqHeight = std::stoi(argv[++i]);
        } else if (arg == "--pipe" && i + 1 < argc) {
            std::string val = argv[++i];
            pipePath = std::wstring(val.begin(), val.end());
        }
    }

    if (!targetHwnd || !IsWindow(targetHwnd)) {
        fprintf(stderr, "[WGC] Error: Invalid or missing target HWND\n");
        return 1;
    }

    _setmode(_fileno(stdout), _O_BINARY);
    SetConsoleCtrlHandler(ConsoleHandler, TRUE);
    timeBeginPeriod(1);

    HANDLE hPipe = INVALID_HANDLE_VALUE;
    if (!pipePath.empty()) {
        hPipe = CreateNamedPipeW(
            pipePath.c_str(),
            PIPE_ACCESS_OUTBOUND,
            PIPE_TYPE_BYTE | PIPE_WAIT,
            1,
            8 * 1024 * 1024, // 8MB buffer
            8 * 1024 * 1024,
            5000,
            nullptr
        );
        if (hPipe == INVALID_HANDLE_VALUE) {
            fprintf(stderr, "[WGC] Error creating named pipe %ls: %lu\n", pipePath.c_str(), GetLastError());
            timeEndPeriod(1);
            return 2;
        }
        fprintf(stderr, "[WGC] Named pipe created: %ls\n", pipePath.c_str());
        fflush(stderr);
    }

    try {
        winrt::init_apartment(winrt::apartment_type::multi_threaded);

        UINT createDeviceFlags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;
        winrt::com_ptr<ID3D11Device> d3d11Device;
        winrt::com_ptr<ID3D11DeviceContext> d3d11Context;
        D3D_FEATURE_LEVEL featureLevel;

        HRESULT hr = D3D11CreateDevice(
            nullptr,
            D3D_DRIVER_TYPE_HARDWARE,
            nullptr,
            createDeviceFlags,
            nullptr,
            0,
            D3D11_SDK_VERSION,
            d3d11Device.put(),
            &featureLevel,
            d3d11Context.put()
        );

        if (FAILED(hr)) {
            fprintf(stderr, "[WGC] Failed to create D3D11 Device (0x%08X)\n", hr);
            if (hPipe != INVALID_HANDLE_VALUE) CloseHandle(hPipe);
            timeEndPeriod(1);
            return 3;
        }

        winrt::com_ptr<IDXGIDevice> dxgiDevice = d3d11Device.as<IDXGIDevice>();
        winrt::com_ptr<IInspectable> inspectableDevice;
        hr = CreateDirect3D11DeviceFromDXGIDevice(dxgiDevice.get(), inspectableDevice.put());
        if (FAILED(hr)) {
            fprintf(stderr, "[WGC] Failed CreateDirect3D11DeviceFromDXGIDevice (0x%08X)\n", hr);
            if (hPipe != INVALID_HANDLE_VALUE) CloseHandle(hPipe);
            timeEndPeriod(1);
            return 4;
        }
        winrt_d3d::IDirect3DDevice winrtDevice = inspectableDevice.as<winrt_d3d::IDirect3DDevice>();

        auto activationFactory = winrt::get_activation_factory<winrt_cap::GraphicsCaptureItem>();
        auto interop = activationFactory.as<IGraphicsCaptureItemInterop>();
        winrt_cap::GraphicsCaptureItem item{ nullptr };
        hr = interop->CreateForWindow(targetHwnd, winrt::guid_of<ABI::Windows::Graphics::Capture::IGraphicsCaptureItem>(), winrt::put_abi(item));
        if (FAILED(hr) || !item) {
            fprintf(stderr, "[WGC] Failed to create GraphicsCaptureItem (0x%08X)\n", hr);
            if (hPipe != INVALID_HANDLE_VALUE) CloseHandle(hPipe);
            timeEndPeriod(1);
            return 5;
        }

        auto itemSize = item.Size();
        if (itemSize.Width <= 0 || itemSize.Height <= 0) {
            fprintf(stderr, "[WGC] Invalid item dimensions %dx%d\n", itemSize.Width, itemSize.Height);
            if (hPipe != INVALID_HANDLE_VALUE) CloseHandle(hPipe);
            timeEndPeriod(1);
            return 6;
        }

        int currentWidth = itemSize.Width;
        int currentHeight = itemSize.Height;
        int outWidth = (reqWidth > 0) ? reqWidth : currentWidth;
        int outHeight = (reqHeight > 0) ? reqHeight : currentHeight;
        size_t outFrameBytes = static_cast<size_t>(outWidth) * outHeight * 4;

        std::vector<uint8_t> frameBuffer(outFrameBytes, 0);
        bool hasValidFrame = false;

        fprintf(stderr, "[WGC] Ready: HWND 0x%p (item: %dx%d, out: %dx%d @ %dfps)\n", targetHwnd, currentWidth, currentHeight, outWidth, outHeight, targetFps);
        fflush(stderr);

        // Wait for connection if pipe was specified
        if (hPipe != INVALID_HANDLE_VALUE) {
            BOOL connected = ConnectNamedPipe(hPipe, nullptr) ? TRUE : (GetLastError() == ERROR_PIPE_CONNECTED);
            if (!connected) {
                fprintf(stderr, "[WGC] ConnectNamedPipe failed: %lu\n", GetLastError());
                CloseHandle(hPipe);
                timeEndPeriod(1);
                return 7;
            }
            fprintf(stderr, "[WGC] Client connected to named pipe\n");
            fflush(stderr);
        }

        auto framePool = winrt_cap::Direct3D11CaptureFramePool::CreateFreeThreaded(
            winrtDevice,
            winrt_dx::DirectXPixelFormat::B8G8R8A8UIntNormalized,
            2,
            itemSize
        );

        auto session = framePool.CreateCaptureSession(item);
        try {
            session.IsCursorCaptureEnabled(false);
        } catch (...) {}
        session.StartCapture();

        winrt::com_ptr<ID3D11Texture2D> stagingTexture;
        auto CreateStagingTexture = [&](int w, int h) -> bool {
            D3D11_TEXTURE2D_DESC desc = {};
            desc.Width = w;
            desc.Height = h;
            desc.MipLevels = 1;
            desc.ArraySize = 1;
            desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM;
            desc.SampleDesc.Count = 1;
            desc.Usage = D3D11_USAGE_STAGING;
            desc.BindFlags = 0;
            desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
            desc.MiscFlags = 0;
            stagingTexture = nullptr;
            HRESULT hrStaging = d3d11Device->CreateTexture2D(&desc, nullptr, stagingTexture.put());
            return SUCCEEDED(hrStaging);
        };

        // Allocate staging texture sized exactly to client dimensions (avoids copying full window chrome to host memory)
        if (!CreateStagingTexture(outWidth, outHeight)) {
            fprintf(stderr, "[WGC] Failed to create staging texture\n");
            if (hPipe != INVALID_HANDLE_VALUE) CloseHandle(hPipe);
            timeEndPeriod(1);
            return 8;
        }

        const auto frameInterval = std::chrono::nanoseconds(1'000'000'000 / targetFps);
        auto nextFrameTime = std::chrono::steady_clock::now();

        while (g_running && IsWindow(targetHwnd)) {
            auto frame = framePool.TryGetNextFrame();
            if (frame) {
                auto newSize = frame.ContentSize();
                if (newSize.Width != currentWidth || newSize.Height != currentHeight) {
                    currentWidth = newSize.Width;
                    currentHeight = newSize.Height;
                    framePool.Recreate(winrtDevice, winrt_dx::DirectXPixelFormat::B8G8R8A8UIntNormalized, 2, newSize);
                    if (reqWidth <= 0 || reqHeight <= 0) {
                        outWidth = currentWidth;
                        outHeight = currentHeight;
                        outFrameBytes = static_cast<size_t>(outWidth) * outHeight * 4;
                        frameBuffer.resize(outFrameBytes, 0);
                        CreateStagingTexture(outWidth, outHeight);
                    }
                }

                auto surface = frame.Surface();
                auto access = surface.as<Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess>();
                winrt::com_ptr<ID3D11Texture2D> frameTexture;
                hr = access->GetInterface(winrt::guid_of<ID3D11Texture2D>(), frameTexture.put_void());

                if (SUCCEEDED(hr) && frameTexture && stagingTexture) {
                    // Calculate client area offsets to crop window title bar and borders
                    int offsetX = 0;
                    int offsetY = 0;
                    if (reqWidth > 0 && reqHeight > 0 && IsWindow(targetHwnd)) {
                        POINT pt = { 0, 0 };
                        ClientToScreen(targetHwnd, &pt);
                        RECT rcWin = {};
                        GetWindowRect(targetHwnd, &rcWin);
                        offsetX = pt.x - rcWin.left;
                        offsetY = pt.y - rcWin.top;
                        if (offsetX < 0) offsetX = 0;
                        if (offsetY < 0) offsetY = 0;
                    }

                    int copyW = std::min(outWidth, std::max(0, currentWidth - offsetX));
                    int copyH = std::min(outHeight, std::max(0, currentHeight - offsetY));

                    if (copyW > 0 && copyH > 0) {
                        // GPU-Side Hardware Cropping: blit client sub-rectangle directly in VRAM via CopySubresourceRegion
                        D3D11_BOX srcBox = {};
                        srcBox.left   = static_cast<UINT>(offsetX);
                        srcBox.top    = static_cast<UINT>(offsetY);
                        srcBox.front  = 0;
                        srcBox.right  = static_cast<UINT>(offsetX + copyW);
                        srcBox.bottom = static_cast<UINT>(offsetY + copyH);
                        srcBox.back   = 1;

                        d3d11Context->CopySubresourceRegion(
                            stagingTexture.get(),
                            0,
                            0, 0, 0,
                            frameTexture.get(),
                            0,
                            &srcBox
                        );

                        D3D11_MAPPED_SUBRESOURCE mapped;
                        hr = d3d11Context->Map(stagingTexture.get(), 0, D3D11_MAP_READ, 0, &mapped);
                        if (SUCCEEDED(hr)) {
                            size_t outRowBytes = static_cast<size_t>(outWidth) * 4;
                            const uint8_t* src = static_cast<const uint8_t*>(mapped.pData);

                            if (mapped.RowPitch == outRowBytes && copyW == outWidth && copyH == outHeight) {
                                // Fast path: contiguous memory block single memcpy
                                memcpy(frameBuffer.data(), src, outFrameBytes);
                            } else {
                                // Fallback row-by-row for non-contiguous pitch
                                size_t copyRowBytes = static_cast<size_t>(copyW) * 4;
                                for (int y = 0; y < copyH; y++) {
                                    memcpy(
                                        frameBuffer.data() + static_cast<size_t>(y) * outRowBytes,
                                        src + static_cast<size_t>(y) * mapped.RowPitch,
                                        copyRowBytes
                                    );
                                }
                            }

                            d3d11Context->Unmap(stagingTexture.get(), 0);
                            hasValidFrame = true;
                        }
                    }
                }
            }

            // Write frame to pipe or stdout if at least one valid frame has been captured.
            // When TryGetNextFrame() returns null (static window), repeats last frame to keep pipe stream active at target FPS.
            bool writeFailed = false;
            if (hasValidFrame) {
                if (hPipe != INVALID_HANDLE_VALUE) {
                    DWORD written = 0;
                    if (!WriteFile(hPipe, frameBuffer.data(), static_cast<DWORD>(outFrameBytes), &written, nullptr)) {
                        writeFailed = true;
                    }
                } else {
                    if (fwrite(frameBuffer.data(), 1, outFrameBytes, stdout) != outFrameBytes) {
                        writeFailed = true;
                    }
                }
            }

            if (writeFailed || (hPipe == INVALID_HANDLE_VALUE && ferror(stdout))) {
                break;
            }

            nextFrameTime += frameInterval;
            auto now = std::chrono::steady_clock::now();
            if (nextFrameTime > now) {
                std::this_thread::sleep_until(nextFrameTime);
            } else {
                nextFrameTime = now;
            }
        }

        session.Close();
        framePool.Close();
    } catch (const winrt::hresult_error& ex) {
        fprintf(stderr, "[WGC] WinRT Exception: %ls (0x%08X)\n", ex.message().c_str(), ex.code().value);
    } catch (const std::exception& ex) {
        fprintf(stderr, "[WGC] Std Exception: %s\n", ex.what());
    }

    if (hPipe != INVALID_HANDLE_VALUE) {
        CloseHandle(hPipe);
    }

    timeEndPeriod(1);
    return 0;
}

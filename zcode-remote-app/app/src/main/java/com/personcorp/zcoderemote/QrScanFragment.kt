package com.personcorp.zcoderemote

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import kotlinx.coroutines.launch
import java.util.concurrent.Executors

/**
 * Full-screen camera + ML Kit barcode overlay. Emits exactly one URL per
 * activation, then hands it to the host. ZCode QRs carry the whole remote
 * URL; anything that is not an https URL is ignored with a toast.
 */
class QrScanFragment : Fragment() {

    interface Listener {
        fun onQrScanned(url: String)
    }

    private val analysisExecutor = Executors.newSingleThreadExecutor()
    private var emitted = false

    private val permissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            if (granted) startCamera() else showPermissionDenied()
        }

    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?,
    ): View = inflater.inflate(R.layout.fragment_qr_scan, container, false)

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        view.findViewById<TextView>(R.id.cancel_button).setOnClickListener {
            parentFragmentManager.popBackStack()
        }
        val granted = ContextCompat.checkSelfPermission(
            requireContext(), Manifest.permission.CAMERA,
        ) == PackageManager.PERMISSION_GRANTED
        if (granted) startCamera() else permissionLauncher.launch(Manifest.permission.CAMERA)
    }

    private fun startCamera() {
        val previewView = requireView().findViewById<PreviewView>(R.id.preview_view)
        val providerFuture = ProcessCameraProvider.getInstance(requireContext())
        providerFuture.addListener({
            try {
                val provider = providerFuture.get()
                val preview = Preview.Builder().build().also {
                    it.setSurfaceProvider(previewView.surfaceProvider)
                }
                val analyzer = ImageAnalysis.Builder()
                    .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
                    .build().also { analysis ->
                        analysis.setAnalyzer(
                            analysisExecutor,
                            QrAnalyzer { url ->
                                if (emitted) return@QrAnalyzer
                                emitted = true
                                viewLifecycleOwnerLiveData.value?.lifecycleScope?.launch {
                                    deliver(url)
                                }
                            },
                        )
                    }
                provider.unbindAll()
                provider.bindToLifecycle(
                    this, CameraSelector.DEFAULT_BACK_CAMERA, preview, analyzer,
                )
            } catch (exc: Exception) {
                Toast.makeText(context, "camera failed: ${exc.message}", Toast.LENGTH_LONG).show()
                parentFragmentManager.popBackStack()
            }
        }, ContextCompat.getMainExecutor(requireContext()))
    }

    private fun deliver(url: String) {
        if (!url.startsWith("https://")) {
            Toast.makeText(context, R.string.not_a_remote_link, Toast.LENGTH_SHORT).show()
            emitted = false
            return
        }
        (activity as? Listener)?.onQrScanned(url) ?: run { emitted = false }
        parentFragmentManager.popBackStack()
    }

    private fun showPermissionDenied() {
        Toast.makeText(context, R.string.camera_permission_needed, Toast.LENGTH_LONG).show()
        parentFragmentManager.popBackStack()
    }

    override fun onDestroy() {
        analysisExecutor.shutdown()
        super.onDestroy()
    }
}

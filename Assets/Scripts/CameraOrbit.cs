using UnityEngine;

public class UltimateCameraSystem : MonoBehaviour
{
    [Header("Refs")]
    public Transform cam;
    public Transform target;
    public Transform core;

    [Header("Block Layer")]
    public LayerMask blockLayer;

    [Header("Rotation")]
    public float minYaw = -30f;
    public float maxYaw = 30f;
    public float minPitch = -30f;
    public float maxPitch = 30f;

    [Header("Zoom")]
    public float distance = 5f;
    public float minDistance = 2f;
    public float maxDistance = 10f;
    public float zoomSpeed = 0.5f;

    [Header("Feel")]
    public float rotationSmooth = 12f;
    public float zoomSmooth = 10f;
    public float rotateSpeed = 8f;
    public float snapStrength = 6f;

    [Header("Inertia")]
    public float inertia = 0.92f;

    // 🎯 target state
    float targetYaw;
    float targetPitch;
    float targetDistance;

    // 🎯 current state
    float yaw;
    float pitch;

    // 🎯 velocity
    float yawVelocity;
    float pitchVelocity;

    bool holding;

    void Start()
    {
        if (!cam) cam = Camera.main.transform;

        Vector3 e = cam.eulerAngles;
        yaw = targetYaw = e.y;
        pitch = targetPitch = e.x;
        targetDistance = distance;
    }

    void Update()
    {
        HandleGate();     // 🎯 Layer控制入口
        HandleInput();    // 🎯 输入
        HandleZoom();
        ApplySpring();    // 🎯 丝滑系统
        ApplyCamera();    // 🎯 输出
    }

    // =========================
    // 🎯 关键：输入权限 Gate
    // =========================
    void HandleGate()
    {
        if (Input.GetMouseButtonDown(0))
        {
            holding = !IsHitBlockLayer();
        }

        if (Input.GetMouseButtonUp(0))
        {
            holding = false;
        }
    }

    // =========================
    // 🎯 Layer检测（不会丢）
    // =========================
    bool IsHitBlockLayer()
    {
        Ray ray = Camera.main.ScreenPointToRay(Input.mousePosition);

        if (Physics.Raycast(ray, out RaycastHit hit, 100f))
        {
            int layer = hit.collider.gameObject.layer;

            return (blockLayer.value & (1 << layer)) != 0;
        }

        return false;
    }

    // =========================
    // 🎯 输入 → 目标值
    // =========================
    void HandleInput()
    {
        if (!holding) return;

        float mx = Input.GetAxis("Mouse X");
        float my = Input.GetAxis("Mouse Y");

        // 目标角度
        targetYaw += mx * rotateSpeed;
        targetPitch -= my * rotateSpeed;

        // zoom
        float scroll = Input.mouseScrollDelta.y;
        targetDistance -= scroll * zoomSpeed;

        // clamp
        targetYaw = Mathf.Clamp(targetYaw, minYaw, maxYaw);
        targetPitch = Mathf.Clamp(targetPitch, minPitch, maxPitch);
        targetDistance = Mathf.Clamp(targetDistance, minDistance, maxDistance);
    }

    // =========================
    // 🎯 丝滑系统（关键统一点）
    // =========================
    void ApplySpring()
    {
        float tRot = 1f - Mathf.Exp(-rotationSmooth * Time.deltaTime);
        float tZoom = 1f - Mathf.Exp(-zoomSmooth * Time.deltaTime);

        // 惯性（轻量保留）
        yawVelocity *= inertia;
        pitchVelocity *= inertia;

        yaw += yawVelocity;
        pitch += pitchVelocity;

        // 平滑目标
        yaw = Mathf.Lerp(yaw, targetYaw, tRot);
        pitch = Mathf.Lerp(pitch, targetPitch, tRot);
        distance = Mathf.Lerp(distance, targetDistance, tZoom);

        // Core吸附（优先级最高）
        if (core != null && !holding)
        {
            Vector3 dir = (core.position - cam.position).normalized;
            Quaternion targetRot = Quaternion.LookRotation(dir);

            Quaternion current = Quaternion.Euler(pitch, yaw, 0);
            current = Quaternion.Slerp(current, targetRot, Time.deltaTime * snapStrength);

            Vector3 e = current.eulerAngles;
            yaw = e.y;
            pitch = e.x;
        }
    }

    // =========================
    // 🎯 输出
    // =========================
    void ApplyCamera()
    {
        Quaternion rot = Quaternion.Euler(pitch, yaw, 0);
        Vector3 pos = target.position + rot * new Vector3(0, 0, -distance);

        cam.position = pos;
        cam.rotation = rot;
    }

    void HandleZoom()
{
    float zoomInput = 0f;

    // =========================
    // 🖥️ 鼠标滚轮
    // =========================
    zoomInput += Input.mouseScrollDelta.y;

    // =========================
    // 📱 双指缩放
    // =========================
    if (Input.touchCount == 2)
    {
        Touch t0 = Input.GetTouch(0);
        Touch t1 = Input.GetTouch(1);

        Vector2 t0Prev = t0.position - t0.deltaPosition;
        Vector2 t1Prev = t1.position - t1.deltaPosition;

        float prevDist = Vector2.Distance(t0Prev, t1Prev);
        float currDist = Vector2.Distance(t0.position, t1.position);

        float delta = currDist - prevDist;

        zoomInput += delta * 0.01f; // 手机缩放系数
    }

    // =========================
    // 🎯 应用缩放（关键）
    // =========================
    if (Mathf.Abs(zoomInput) > 0.0001f)
    {
        targetDistance -= zoomInput * zoomSpeed;
        targetDistance = Mathf.Clamp(targetDistance, minDistance, maxDistance);
    }
}
}